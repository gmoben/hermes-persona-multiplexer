"""Tests for the router's inbound gating + outbound persona resolution.

``connect()`` and the actual send/media/typing delegation need a live Hermes
install with the bundled Discord adapter, so they're integration-validated. The
pure wiring is testable by constructing the adapter via ``__new__`` and feeding
it a stub event:

* ``_dispatch_inbound`` — gate a delegate-built event + stamp the persona prompt
  (the delegate sends its own reply, so the chat id is left untouched);
* ``_persona_for_outbound`` — which persona owns a gateway-initiated outbound.
"""

import asyncio
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hermes_persona_multiplexer import adapter as a
from hermes_persona_multiplexer.routing import parse_config


def _cfg():
    return parse_config(
        {
            "brain": "main",
            "default_persona": "alex",
            "personas": [
                {"id": "alex", "token_env": "T_ALEX", "display_name": "Alex"},
                {"id": "sam", "token_env": "T_SAM"},
            ],
        }
    )


class _Src:
    """Stand-in for SessionSource — just holds attributes."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Event:
    """Stand-in for a delegate-built MessageEvent."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _make_router(own=(), home=None):  # bypass BasePlatformAdapter.__init__
    ad = a.DiscordPersonasAdapter.__new__(a.DiscordPersonasAdapter)
    ad.mux = _cfg()
    ad._own_account_ids = set(own)
    ad._home_channel_id = home
    ad._reply_persona = {}
    ad._chat_persona = {}
    ad._delegates = {}
    ad._orig = {}
    ad._typing_delegate = {}
    ad._typing_paused = set()
    ad._allowed_users = frozenset()  # open by default
    ad._disconnecting = False
    ad._fatal_forwarded = False
    ad.platform = "discord_personas"
    ad.handled = []

    async def _handler(event):
        ad.handled.append(event)
        return None

    ad._message_handler = _handler
    return ad


def _event(chat_id="42", chat_type="dm", user_id="u1"):
    return _Event(source=_Src(chat_id=chat_id, chat_type=chat_type, user_id=user_id),
                  channel_prompt=None)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("is_reconnect", [False, True])
def test_connect_supports_new_gateway_and_legacy_delegates(monkeypatch, legacy, is_reconnect):
    ad = _make_router()
    delegates = []
    flags = []

    def build_delegate(token):
        if legacy:
            async def connect():
                flags.append(None)
                return True
        else:
            async def connect(*, is_reconnect=False):
                flags.append(is_reconnect)
                return True

        delegate = SimpleNamespace(
            connect=connect,
            set_message_handler=Mock(),
            _ready_event=SimpleNamespace(wait=AsyncMock()),
            _client=SimpleNamespace(user=SimpleNamespace(id=len(delegates))),
            **{name: AsyncMock() for name in a._ROUTED_METHODS},
        )
        if not legacy:
            delegate.set_fatal_error_handler = Mock()
        delegates.append(delegate)
        return delegate

    monkeypatch.setattr(a, "_DISCORD_AVAILABLE", True)
    monkeypatch.setattr(a, "_DELEGATE_AVAILABLE", True)
    monkeypatch.setenv("T_ALEX", "synthetic-alex")
    monkeypatch.setenv("T_SAM", "synthetic-sam")
    ad._build_delegate = build_delegate
    ad._mark_connected = Mock()

    assert asyncio.run(ad.connect(is_reconnect=is_reconnect))
    assert flags == [None if legacy else is_reconnect] * 2
    assert set(ad._delegates) == {"alex", "sam"}
    ad._mark_connected.assert_called_once_with()
    for delegate in delegates:
        assert delegate.platform == "discord_personas"
        delegate.set_message_handler.assert_called_once()
        if not legacy:
            delegate.set_fatal_error_handler.assert_called_once()


@pytest.mark.parametrize("retryable", [False, True])
def test_delegate_fatal_error_notifies_runner_through_router(retryable):
    ad = _make_router()
    delegate = SimpleNamespace(
        fatal_error_code="discord_websocket_health_stale", fatal_error_retryable=retryable,
    )
    ad._delegates["alex"] = delegate
    ad._set_fatal_error = Mock()
    ad._notify_fatal_error = AsyncMock()

    asyncio.run(ad._make_delegate_fatal_error_handler("alex")(delegate))

    ad._set_fatal_error.assert_called_once_with(
        "discord_persona_failed",
        "Discord persona 'alex' failed (discord_websocket_health_stale)",
        retryable=True,
    )
    ad._notify_fatal_error.assert_awaited_once_with()


def test_concurrent_delegate_failures_notify_runner_once():
    ad = _make_router()
    ad._delegates = {"alex": SimpleNamespace(), "sam": SimpleNamespace()}
    ad._set_fatal_error = Mock()
    ad._notify_fatal_error = AsyncMock()

    async def fail():
        await asyncio.gather(*(
            ad._make_delegate_fatal_error_handler(pid)(delegate)
            for pid, delegate in ad._delegates.items()
        ))

    asyncio.run(fail())

    ad._set_fatal_error.assert_called_once()
    ad._notify_fatal_error.assert_awaited_once()


@pytest.mark.parametrize("stale,disconnecting", [(True, False), (False, True)])
def test_delegate_fatal_error_ignored_after_replacement_or_during_teardown(stale, disconnecting):
    ad = _make_router()
    delegate = SimpleNamespace()
    ad._delegates["alex"] = SimpleNamespace() if stale else delegate
    ad._disconnecting = disconnecting
    ad._set_fatal_error = Mock()
    ad._notify_fatal_error = AsyncMock()

    asyncio.run(ad._make_delegate_fatal_error_handler("alex")(delegate))

    ad._set_fatal_error.assert_not_called()
    ad._notify_fatal_error.assert_not_awaited()


def test_connect_detects_delegate_failure_during_startup(monkeypatch):
    ad = _make_router()
    delegates = []

    def build_delegate(token):
        async def connect(*, is_reconnect=False):
            # The first persona dies while the second is connecting.
            if len(delegates) == 2:
                delegates[0].has_fatal_error = True
            return True

        delegate = SimpleNamespace(
            connect=connect, has_fatal_error=False,
            fatal_error_code="discord_websocket_health_stale",
            set_message_handler=Mock(), set_fatal_error_handler=Mock(),
            _ready_event=SimpleNamespace(wait=AsyncMock()),
            _client=SimpleNamespace(user=SimpleNamespace(id=len(delegates))),
            **{name: AsyncMock() for name in a._ROUTED_METHODS},
        )
        delegates.append(delegate)
        return delegate

    monkeypatch.setattr(a, "_DISCORD_AVAILABLE", True)
    monkeypatch.setattr(a, "_DELEGATE_AVAILABLE", True)
    monkeypatch.setenv("T_ALEX", "synthetic-alex")
    monkeypatch.setenv("T_SAM", "synthetic-sam")
    ad._build_delegate = build_delegate
    ad._set_fatal_error = Mock()
    ad._mark_connected = Mock()
    ad.disconnect = AsyncMock()

    assert not asyncio.run(ad.connect())

    ad._set_fatal_error.assert_called_once()
    ad._mark_connected.assert_not_called()
    ad.disconnect.assert_awaited_once()
    for delegate in delegates:
        delegate.set_fatal_error_handler.assert_not_called()


def test_disconnect_detaches_delegate_failure_handlers_before_closing():
    ad = _make_router()
    ad._set_fatal_error = Mock()
    ad._notify_fatal_error = AsyncMock()
    ad._mark_disconnected = Mock()

    async def close():
        assert ad._disconnecting
        delegate.set_fatal_error_handler.assert_called_once_with(None)
        await ad._make_delegate_fatal_error_handler("alex")(delegate)

    delegate = SimpleNamespace(set_fatal_error_handler=Mock(), disconnect=close)
    ad._delegates["alex"] = delegate

    asyncio.run(ad.disconnect())

    ad._set_fatal_error.assert_not_called()
    ad._notify_fatal_error.assert_not_awaited()
    ad._mark_disconnected.assert_called_once()
    assert not ad._delegates


def _intake_fixture(monkeypatch, *, persona="alex", is_dm=False):
    class DMChannel:
        id = "private-dm-fixture"

    message_types = SimpleNamespace(default=object(), reply=object())
    monkeypatch.setattr(a, "discord", SimpleNamespace(
        DMChannel=DMChannel, MessageType=message_types,
    ))
    ad = _make_router(home="private-channel-fixture")
    ad._allowed_users = frozenset({"private-user-fixture"})
    seen = set()

    def duplicate(message_id):
        if message_id in seen:
            return True
        seen.add(message_id)
        return False

    delegate = SimpleNamespace(
        _dedup=SimpleNamespace(is_duplicate=duplicate),
        _client=SimpleNamespace(user=SimpleNamespace(id="private-bot-fixture")),
        _handle_message=AsyncMock(return_value=True),
    )
    message = SimpleNamespace(
        id="private-message-fixture",
        author=SimpleNamespace(id="private-user-fixture", bot=False),
        channel=DMChannel() if is_dm else SimpleNamespace(id=ad._home_channel_id),
        type=message_types.default,
        attachments=[SimpleNamespace(url="https://example.invalid/private-photo.jpg")],
        content="private-caption-fixture",
    )
    return ad, delegate, message, ad._make_on_message(persona, delegate)


@pytest.mark.parametrize("is_dm", [False, True])
def test_live_intake_dispatches_duplicate_event_once(monkeypatch, caplog, is_dm):
    ad, delegate, message, intake = _intake_fixture(monkeypatch, is_dm=is_dm)

    async def deliver_twice():
        await asyncio.gather(intake(message), intake(message))

    with caplog.at_level(logging.INFO, logger=a.__name__):
        asyncio.run(deliver_twice())

    delegate._handle_message.assert_awaited_once_with(message)
    assert "stage=delegate_dispatch" in caplog.text
    assert "stage=duplicate" in caplog.text
    assert "home_channel=True" in caplog.text
    for private in [message.id, message.author.id, message.content, message.channel.id,
                    message.attachments[0].url]:
        assert private not in caplog.text


def test_live_shared_channel_intake_runs_only_on_orchestrator(monkeypatch):
    _, delegate, message, intake = _intake_fixture(monkeypatch, persona="sam")

    asyncio.run(intake(message))

    delegate._handle_message.assert_not_awaited()


def test_live_intake_rejection_is_visible_without_logging_identities(monkeypatch, caplog):
    _, delegate, message, intake = _intake_fixture(monkeypatch)
    delegate._handle_message.return_value = False

    with caplog.at_level(logging.INFO, logger=a.__name__):
        asyncio.run(intake(message))

    assert "stage=delegate_rejected" in caplog.text
    assert message.id not in caplog.text
    assert message.author.id not in caplog.text


def test_socket_trace_preserves_native_liveness_callback(caplog):
    ad = _make_router()
    native = AsyncMock()
    handler = ad._make_socket_event_handler("alex", native)

    async def receive():
        await handler("HEARTBEAT_ACK")
        await handler("MESSAGE_CREATE")

    with caplog.at_level(logging.INFO, logger=a.__name__):
        asyncio.run(receive())

    assert native.await_count == 2
    native.assert_any_await("HEARTBEAT_ACK")
    native.assert_any_await("MESSAGE_CREATE")
    assert caplog.text.count("stage=gateway_message_create") == 1
    assert "HEARTBEAT_ACK" not in caplog.text


def test_delegate_disables_native_reexecution_but_keeps_history_context(monkeypatch):
    ad = _make_router()
    ad.config = SimpleNamespace(reply_to_mode="first")
    monkeypatch.setattr(a, "PlatformConfig", SimpleNamespace, raising=False)
    native = Mock()
    monkeypatch.setattr(a, "DiscordAdapter", native)

    ad._build_delegate("synthetic-not-a-credential")

    cfg = native.call_args.args[0]
    assert cfg.extra["history_backfill"] is True
    assert cfg.extra["missed_message_backfill"] == {"enabled": False}


def test_delegate_routing_flags_survive_gateway_exported_discord_defaults(monkeypatch):
    ad = _make_router()
    ad.config = SimpleNamespace(reply_to_mode="first")
    monkeypatch.setattr(a, "PlatformConfig", SimpleNamespace, raising=False)
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    calls = []

    def native_flag(key, env_key, env_default, *, truthy):
        calls.append(key)
        return os.environ.get(env_key, env_default).lower() == "true"

    native = SimpleNamespace(_extra_or_env_flag=native_flag)
    monkeypatch.setattr(a, "DiscordAdapter", lambda cfg: native)

    delegate = ad._build_delegate("synthetic-not-a-credential")

    assert not delegate._extra_or_env_flag(
        "require_mention", "DISCORD_REQUIRE_MENTION", "true", truthy=False,
    )
    assert not delegate._extra_or_env_flag(
        "auto_thread", "DISCORD_AUTO_THREAD", "true", truthy=True,
    )
    # Unrelated native policy remains native; no access gate is forced open.
    assert not delegate._extra_or_env_flag(
        "unrelated_policy", "SYNTHETIC_UNRELATED_FLAG", "false", truthy=True,
    )
    monkeypatch.setenv("SYNTHETIC_UNRELATED_FLAG", "true")
    assert delegate._extra_or_env_flag(
        "unrelated_policy", "SYNTHETIC_UNRELATED_FLAG", "false", truthy=True,
    )
    assert calls == ["unrelated_policy", "unrelated_policy"]
    assert os.environ["DISCORD_REQUIRE_MENTION"] == "true"
    assert os.environ["DISCORD_AUTO_THREAD"] == "true"


def test_persona_channel_prompt_names_the_persona():
    prompt = a.persona_channel_prompt("Alex", "alex")
    assert "Alex" in prompt
    assert "alex" in prompt
    assert "voice" in prompt.lower()


# ── inbound gating + persona prompt (_dispatch_inbound) ─────────────────────
def test_dispatch_inbound_dm_processes_and_prompts():
    ad = _make_router()
    ev = _event(chat_id="42", chat_type="dm")
    asyncio.run(ad._dispatch_inbound("alex", ev))
    assert len(ad.handled) == 1
    ev2 = ad.handled[0]
    # delegate owns the send, so the chat id is left untouched (NOT persona-encoded)
    assert ev2.source.chat_id == "42"
    assert "alex" in ev2.channel_prompt.lower()
    # DM ownership recorded so outbound media/typing route back to alex
    assert ad._chat_persona["42"] == "alex"


def test_dispatch_inbound_dm_records_addressed_persona():
    ad = _make_router()
    asyncio.run(ad._dispatch_inbound("sam", _event(chat_id="9", chat_type="dm")))
    assert ad._chat_persona["9"] == "sam"


def test_dispatch_inbound_drops_own_account():
    ad = _make_router(own=("999",))  # author is one of our own persona bots
    asyncio.run(ad._dispatch_inbound("alex", _event(user_id="999")))
    assert ad.handled == []


def test_dispatch_inbound_unknown_persona_dropped():
    ad = _make_router()
    asyncio.run(ad._dispatch_inbound("ghost", _event()))
    assert ad.handled == []


def test_dispatch_inbound_allowlist_blocks_other_users():
    ad = _make_router()
    ad._allowed_users = frozenset({"217770140723445760"})  # only Ben
    asyncio.run(ad._dispatch_inbound("alex", _event(user_id="999")))  # someone else
    assert ad.handled == []


def test_dispatch_inbound_allowlist_admits_listed_user():
    ad = _make_router()
    ad._allowed_users = frozenset({"217770140723445760"})
    asyncio.run(ad._dispatch_inbound("alex", _event(user_id="217770140723445760")))
    assert len(ad.handled) == 1


# ── shared-channel intake ───────────────────────────────────────────────────
def test_dispatch_inbound_channel_orchestrator_intakes():
    ad = _make_router(home="777")
    ev = _event(chat_id="777", chat_type="group")
    asyncio.run(ad._dispatch_inbound("alex", ev))  # alex == default => orchestrator
    assert len(ad.handled) == 1
    ev2 = ad.handled[0]
    assert ev2.source.chat_id == "777"  # untouched
    # shared-channel prompt drives the ack's [next:] hint + the reply [persona:] tag
    assert "[persona:" in ev2.channel_prompt and "[next:" in ev2.channel_prompt


def test_dispatch_inbound_channel_thread_under_home_intakes():
    # auto-thread: chat_id is the new thread, parent_chat_id is the home channel —
    # gate against the parent so the orchestrator's threaded turn isn't dropped.
    ad = _make_router(home="777")
    ev = _Event(source=_Src(chat_id="thread-9", chat_type="thread", user_id="u1",
                            parent_chat_id="777"), channel_prompt=None)
    asyncio.run(ad._dispatch_inbound("alex", ev))
    assert len(ad.handled) == 1
    assert "[persona:" in ad.handled[0].channel_prompt  # shared (orchestrator) prompt


def test_dispatch_inbound_channel_non_orchestrator_dropped():
    ad = _make_router(home="777")
    asyncio.run(ad._dispatch_inbound("sam", _event(chat_id="777", chat_type="group")))
    assert ad.handled == []  # only the orchestrator intakes the shared channel


def test_dispatch_inbound_channel_outside_home_dropped():
    ad = _make_router(home="777")
    asyncio.run(ad._dispatch_inbound("alex", _event(chat_id="999", chat_type="group")))
    assert ad.handled == []


# ── chat-id codec ───────────────────────────────────────────────────────────
def test_decode_roundtrip():
    assert a.encode_chat_id("alex", "42") == "p!alex!42"
    assert a.decode_chat_id("p!alex!42") == ("alex", "42")
    assert a.decode_chat_id("p!sam!c-1") == ("sam", "c-1")
    assert a.decode_chat_id("plain") == (None, "plain")


# ── outbound persona resolution (media/typing/cron) ─────────────────────────
def test_persona_for_outbound_encoded_hint_wins():
    ad = _make_router()
    assert ad._persona_for_outbound("p!sam!42") == ("sam", "42")  # cron-style encoded target


def test_persona_for_outbound_channel_prefers_answering_persona():
    ad = _make_router(home="777")
    ad._reply_persona = {"777": "sam"}  # the turn's [persona:] tag chose sam
    assert ad._persona_for_outbound("777") == ("sam", "777")


def test_persona_for_outbound_dm_uses_owning_persona():
    ad = _make_router()
    ad._chat_persona = {"42": "sam"}  # recorded when sam's DM came in
    assert ad._persona_for_outbound("42") == ("sam", "42")


def test_persona_for_outbound_unknown_uses_default():
    ad = _make_router()
    assert ad._persona_for_outbound("42") == ("alex", "42")  # default_persona


def test_persona_for_outbound_home_channel_falls_back_to_orchestrator():
    # Pre-ack, nothing is routed yet — gateway-initiated typing in the home channel
    # belongs to the orchestrator (who authors the ack), not default_persona.
    ad = _make_router(home="777")
    ad.mux = parse_config(
        {
            "brain": "main",
            "default_persona": "alex",
            "orchestrator": "sam",
            "personas": [
                {"id": "alex", "token_env": "T_ALEX"},
                {"id": "sam", "token_env": "T_SAM"},
            ],
        }
    )
    assert ad._persona_for_outbound("777") == ("sam", "777")   # home -> orchestrator
    assert ad._persona_for_outbound("42") == ("alex", "42")    # elsewhere -> default


# ── delegate typing interception (the single-typer fix) ─────────────────────
def _typing_recorder(ad):
    calls = []

    async def _rec(pid, raw, metadata=None):
        calls.append((pid, raw))

    ad._start_typing_as = _rec
    return calls


def test_delegate_typing_pre_ack_types_as_the_intaking_delegate():
    # The orchestrator's _keep_typing refresh, before any [next:] tag: he is about
    # to send the ack, so he is the correct typer.
    ad = _make_router(home="777")
    calls = _typing_recorder(ad)
    asyncio.run(ad._make_delegate_typing("alex")("777"))
    assert calls == [("alex", "777")]


def test_delegate_typing_follows_next_tag_not_the_caller():
    # Post-ack: the orchestrator's processing loop keeps refreshing, but the
    # [next:]-named persona must be the (only) typer — this is the double-typing bug.
    ad = _make_router(home="777")
    ad._reply_persona["777"] = "sam"
    calls = _typing_recorder(ad)
    asyncio.run(ad._make_delegate_typing("alex")("777"))  # caller=alex, answerer=sam
    assert calls == [("sam", "777")]


def test_delegate_typing_dm_uses_owning_persona():
    ad = _make_router()
    ad._chat_persona["9"] = "sam"
    calls = _typing_recorder(ad)
    asyncio.run(ad._make_delegate_typing("sam")("9"))
    assert calls == [("sam", "9")]


def test_delegate_typing_unknown_reply_persona_falls_back_to_caller():
    ad = _make_router(home="777")
    ad._reply_persona["777"] = "ghost"  # not a configured persona
    calls = _typing_recorder(ad)
    asyncio.run(ad._make_delegate_typing("alex")("777"))
    assert calls == [("alex", "777")]
