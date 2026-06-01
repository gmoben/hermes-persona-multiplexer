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
    ad._allowed_users = frozenset()  # open by default
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
