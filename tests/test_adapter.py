"""Tests for the live-wiring decision path in the adapter.

``connect()``/``send()`` need a real Discord loop, but ``_on_inbound`` — the
inbound normalization + loop-guard wiring — is testable by stubbing the two
Hermes types it references (``MessageEvent``/``MessageType``) and overriding the
inherited ``build_source``/``handle_message``. No Hermes or discord.py needed.
"""

import asyncio

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


class _Event:
    """Stand-in for gateway MessageEvent — just captures kwargs."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _make_adapter(monkeypatch, own=()):  # bypass BasePlatformAdapter.__init__
    monkeypatch.setattr(a, "MessageEvent", _Event, raising=False)
    monkeypatch.setattr(a, "MessageType", type("MT", (), {"TEXT": "text"}), raising=False)
    ad = a.DiscordPersonasAdapter.__new__(a.DiscordPersonasAdapter)
    ad.mux = _cfg()
    ad._own_account_ids = set(own)
    ad._account_to_persona = {}
    ad._home_channel_id = None
    ad.handled = []
    ad.build_source = lambda **kw: kw  # SessionSource stand-in: the kwargs dict
    async def _handle(event):
        ad.handled.append(event)
    ad.handle_message = _handle
    return ad


def test_persona_channel_prompt_names_the_persona():
    prompt = a.persona_channel_prompt("Alex", "alex")
    assert "Alex" in prompt
    assert "alex" in prompt
    assert "voice" in prompt.lower()


def test_on_inbound_processes_and_tags_persona(monkeypatch):
    ad = _make_adapter(monkeypatch)
    asyncio.run(
        ad._on_inbound(
            recipient_persona="alex",
            author_account_id="999",
            raw_chat_id="42",
            text="hi",
            message_id="1",
            chat_type="dm",
            chat_name="user",
            user_name="user",
        )
    )
    assert len(ad.handled) == 1
    ev = ad.handled[0]
    assert ev.text == "hi"
    # chat_id is persona-namespaced so outbound routes back through 'alex'
    assert ev.source["chat_id"] == "p!alex!42"
    assert "alex" in ev.channel_prompt.lower()


def test_on_inbound_drops_self_authored(monkeypatch):
    # author 999 is one of our own persona bots -> loop guard drops it
    ad = _make_adapter(monkeypatch, own=("999",))
    asyncio.run(
        ad._on_inbound(
            recipient_persona="alex",
            author_account_id="999",
            raw_chat_id="42",
            text="echo",
            message_id="1",
            chat_type="dm",
            chat_name="user",
            user_name="user",
        )
    )
    assert ad.handled == []


def test_on_inbound_unknown_persona_is_dropped(monkeypatch):
    ad = _make_adapter(monkeypatch)
    asyncio.run(
        ad._on_inbound(
            recipient_persona="nobody",
            author_account_id="999",
            raw_chat_id="42",
            text="hi",
            message_id="1",
            chat_type="dm",
            chat_name="user",
            user_name="user",
        )
    )
    assert ad.handled == []


@pytest.mark.parametrize(
    "enc,persona,raw",
    [
        ("p!alex!42", "alex", "42"),
        ("p!sam!c-1", "sam", "c-1"),
    ],
)
def test_decode_roundtrip(enc, persona, raw):
    assert a.encode_chat_id(persona, raw) == enc
    assert a.decode_chat_id(enc) == (persona, raw)


# ── shared-channel intake ──────────────────────────────────────────────────
def test_on_inbound_channel_orchestrator_intakes(monkeypatch):
    ad = _make_adapter(monkeypatch)
    ad._home_channel_id = "777"
    asyncio.run(
        ad._on_inbound(
            recipient_persona="alex",  # default => orchestrator
            author_account_id="u1", raw_chat_id="777", text="a photo",
            message_id="1", chat_type="group", chat_name="general", user_name="u",
        )
    )
    assert len(ad.handled) == 1
    ev = ad.handled[0]
    # shared-channel prompt instructs a quick (untagged) ack, then the routing tag,
    # and namespaces the session to the orchestrator
    assert "[persona:" in ev.channel_prompt
    assert "no persona tag" in ev.channel_prompt.lower()
    assert ev.source["chat_id"] == "p!alex!777"


def test_on_inbound_channel_non_orchestrator_dropped(monkeypatch):
    ad = _make_adapter(monkeypatch)
    ad._home_channel_id = "777"
    asyncio.run(
        ad._on_inbound(
            recipient_persona="sam",  # not the orchestrator -> dedup drop
            author_account_id="u1", raw_chat_id="777", text="hi",
            message_id="1", chat_type="group", chat_name="general", user_name="u",
        )
    )
    assert ad.handled == []


def test_on_inbound_channel_outside_home_dropped(monkeypatch):
    ad = _make_adapter(monkeypatch)
    ad._home_channel_id = "777"
    asyncio.run(
        ad._on_inbound(
            recipient_persona="alex", author_account_id="u1", raw_chat_id="999",
            text="hi", message_id="1", chat_type="group", chat_name="other", user_name="u",
        )
    )
    assert ad.handled == []
