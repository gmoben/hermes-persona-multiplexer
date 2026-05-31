"""Tests for the plugin registration entry point and chat-id codec.

These import ``hermes_persona_multiplexer.adapter`` directly — safe even without
Hermes, because the Hermes/discord imports in that module are guarded.
"""

from hermes_persona_multiplexer import adapter as a
from hermes_persona_multiplexer.routing import PLATFORM_NAME


class FakeCtx:
    def __init__(self):
        self.calls = []

    def register_platform(self, **kwargs):
        self.calls.append(kwargs)


def test_register_registers_platform():
    ctx = FakeCtx()
    name = a.register(ctx)
    assert name == PLATFORM_NAME
    assert len(ctx.calls) == 1
    kw = ctx.calls[0]
    assert kw["name"] == "discord_personas"
    assert callable(kw["adapter_factory"])
    assert callable(kw["check_fn"])
    assert callable(kw["standalone_sender_fn"])
    assert kw["cron_deliver_env_var"] == "DISCORD_PERSONAS_HOME_CHANNEL"
    assert kw["allowed_users_env"] == "DISCORD_PERSONAS_ALLOWED_USERS"
    assert kw["max_message_length"] == 2000
    assert "persona" in kw["platform_hint"].lower()


def test_top_level_register_delegates():
    # hermes_persona_multiplexer.register() should forward to adapter.register()
    from hermes_persona_multiplexer import register as pkg_register

    ctx = FakeCtx()
    assert pkg_register(ctx) == PLATFORM_NAME
    assert ctx.calls[0]["name"] == "discord_personas"


def test_chat_id_codec_roundtrip():
    enc = a.encode_chat_id("alex", "123456789")
    persona, raw = a.decode_chat_id(enc)
    assert persona == "alex"
    assert raw == "123456789"


def test_chat_id_codec_unprefixed():
    persona, raw = a.decode_chat_id("987654321")
    assert persona is None
    assert raw == "987654321"
