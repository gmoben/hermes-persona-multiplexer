"""Tests for the router's persona-tagging + outbound resolution.

``connect()``/``send()``/the ``send_*`` delegations need a live Hermes install
with the bundled Discord adapter, so they're integration-validated. The pure
wiring — ``_dispatch_inbound`` (re-tag a delegate-built event with its persona)
and ``_persona_for_outbound`` (which persona owns an outbound action) — is
testable by constructing the adapter via ``__new__`` and feeding it a stub event.
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
    ad._delegates = {}
    ad.platform = "discord_personas"  # only read as src.platform in re-tag
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


# ── inbound re-tagging (_dispatch_inbound) ──────────────────────────────────
def test_dispatch_inbound_dm_tags_persona():
    ad = _make_router()
    ev = _event(chat_id="42", chat_type="dm")
    asyncio.run(ad._dispatch_inbound("alex", ev))
    assert len(ad.handled) == 1
    ev2 = ad.handled[0]
    # chat_id is persona-namespaced so outbound routes back through 'alex'
    assert ev2.source.chat_id == "p!alex!42"
    # replies route back through the router, not the delegate
    assert ev2.source.platform == "discord_personas"
    assert "alex" in ev2.channel_prompt.lower()


def test_dispatch_inbound_dm_uses_addressed_persona():
    ad = _make_router()
    asyncio.run(ad._dispatch_inbound("sam", _event(chat_id="9", chat_type="dm")))
    assert ad.handled[0].source.chat_id == "p!sam!9"


def test_dispatch_inbound_drops_own_account():
    # author 999 is one of our own persona bots -> loop guard drops it
    ad = _make_router(own=("999",))
    asyncio.run(ad._dispatch_inbound("alex", _event(user_id="999")))
    assert ad.handled == []


def test_dispatch_inbound_unknown_persona_dropped():
    ad = _make_router()
    asyncio.run(ad._dispatch_inbound("ghost", _event()))
    assert ad.handled == []


# ── shared-channel intake ───────────────────────────────────────────────────
def test_dispatch_inbound_channel_orchestrator_intakes():
    ad = _make_router(home="777")
    ev = _event(chat_id="777", chat_type="group")
    asyncio.run(ad._dispatch_inbound("alex", ev))  # alex == default => orchestrator
    assert len(ad.handled) == 1
    ev2 = ad.handled[0]
    assert ev2.source.chat_id == "p!alex!777"
    # shared-channel prompt drives the ack's [next:] hint + the reply [persona:] tag
    assert "[persona:" in ev2.channel_prompt and "[next:" in ev2.channel_prompt


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


# ── outbound attachment / typing persona routing ────────────────────────────
def test_persona_for_outbound_dm_uses_decoded_persona():
    ad = _make_router()
    assert ad._persona_for_outbound("p!sam!42") == ("sam", "42")


def test_persona_for_outbound_channel_prefers_answering_persona():
    ad = _make_router(home="777")
    ad._reply_persona = {"777": "sam"}  # the turn's [persona:] tag chose sam
    # decoded id is the orchestrator (alex), but the reply — and its attachment — is sam's
    assert ad._persona_for_outbound("p!alex!777") == ("sam", "777")


def test_persona_for_outbound_channel_without_reply_uses_decoded():
    ad = _make_router(home="777")
    assert ad._persona_for_outbound("p!alex!777") == ("alex", "777")


def test_persona_for_outbound_unknown_persona_uses_default():
    ad = _make_router()
    assert ad._persona_for_outbound("p!ghost!42") == ("alex", "42")  # default_persona
