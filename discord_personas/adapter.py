"""Hermes gateway wiring for the persona multiplexer.

This module connects the pure decision logic in :mod:`discord_personas.routing`
and :mod:`discord_personas.locks` to the Hermes gateway and ``discord.py``.

It runs **N Discord clients in one process** (one per persona/bot token), tags
every inbound event with the persona whose account received it, and forwards all
of them to the single gateway runner — i.e. one shared-brain agent. Outbound
replies are routed back through the originating persona's client so DMs reply as
the right persona (e.g. @Alex, @Sam) and channel messages post as the right account.

Hermes and ``discord.py`` imports are guarded so the package remains importable
(for unit-testing the core) on machines without Hermes installed. The methods
that need a live gateway/network are marked ``TODO(spike)`` — they are validated
by the two-account spike described in ``docs/spike.md``.
"""

from __future__ import annotations

import logging
import os

from .locks import ScopedLockManager
from .routing import (
    PLATFORM_NAME,
    MultiplexerConfig,
    decide_inbound,
    parse_config,
    persona_metadata,
    resolve_outbound_persona,
)

logger = logging.getLogger(__name__)

try:  # pragma: no cover - exercised only inside a Hermes install
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult

    _HERMES_AVAILABLE = True
except Exception:  # noqa: BLE001
    _HERMES_AVAILABLE = False

    class BasePlatformAdapter:  # type: ignore[no-redef]
        """Import shim so this module loads without Hermes (tests/CI)."""

        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "discord_personas adapter requires Hermes Agent at runtime "
                "(gateway.platforms.base could not be imported)."
            )


# Delimiter used to namespace a persona into a Discord chat id so the persona
# round-trips through Hermes session keys without a separate sidecar store.
# Each persona DM/channel becomes its own session, but they share the profile's
# memory — one brain, many faces.
PERSONA_PREFIX = "p!"


def encode_chat_id(persona_id: str, raw_chat_id: str) -> str:
    return f"{PERSONA_PREFIX}{persona_id}!{raw_chat_id}"


def decode_chat_id(chat_id: str) -> tuple[str | None, str]:
    """Return ``(persona_id, raw_chat_id)``; persona is ``None`` if unprefixed."""
    if chat_id.startswith(PERSONA_PREFIX):
        body = chat_id[len(PERSONA_PREFIX):]
        persona_id, _, raw = body.partition("!")
        if persona_id and raw:
            return persona_id, raw
    return None, chat_id


class DiscordPersonasAdapter(BasePlatformAdapter):
    """Multiplexing Discord adapter: N bot accounts -> one shared-brain agent."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM_NAME))
        extra = getattr(config, "extra", None) or {}
        self.mux: MultiplexerConfig = parse_config(extra.get(PLATFORM_NAME, extra))
        self.locks = ScopedLockManager(
            PLATFORM_NAME,
            acquire=_real_acquire_lock,
            release=_real_release_lock,
        )
        self._clients: dict[str, object] = {}          # persona_id -> discord client
        self._own_account_ids: set[str] = set()         # bot user ids of all personas
        self._tokens: dict[str, str] = {}               # persona_id -> token value

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> bool:  # pragma: no cover - needs live env
        """Start one Discord client per persona; degrade per-persona on failure.

        TODO(spike): instantiate the bundled Hermes Discord adapter (or a raw
        ``discord.py`` client) per token, wire ``on_message`` -> ``_on_inbound``,
        capture each bot's own user id into ``self._own_account_ids``.
        """
        started = 0
        for persona in self.mux.personas:
            token = os.getenv(persona.token_env, "").strip()
            if not token:
                logger.warning("[%s] persona '%s' has no token in %s; skipping",
                               PLATFORM_NAME, persona.id, persona.token_env)
                continue
            lock = self.locks.acquire(persona.id, token)
            if not lock.acquired:
                logger.warning("[%s] persona '%s' degraded: %s",
                               PLATFORM_NAME, persona.id, lock.error)
                continue
            self._tokens[persona.id] = token
            # TODO(spike): client = build_persona_client(persona, token, on_message=...)
            #              self._clients[persona.id] = client; await client.start()
            #              self._own_account_ids.add(str(client.user.id))
            started += 1
        if started == 0:
            logger.error("[%s] no personas could start", PLATFORM_NAME)
            return False
        self._mark_connected()
        logger.info("[%s] online personas=%s degraded=%s",
                    PLATFORM_NAME, self.locks.active_personas, self.locks.degraded_personas)
        return True

    async def disconnect(self) -> None:  # pragma: no cover - needs live env
        for persona_id, client in list(self._clients.items()):
            try:
                await client.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                logger.debug("[%s] error closing persona '%s'", PLATFORM_NAME, persona_id)
        self.locks.release_all()
        self._mark_disconnected()

    # -- inbound -----------------------------------------------------------
    def _on_inbound(self, *, recipient_persona: str, author_account_id: str | None,
                    raw_chat_id: str, **event_fields):  # pragma: no cover - needs live env
        """Normalize a discord message into a persona-tagged ``MessageEvent``.

        Loop prevention + persona resolution are delegated to the pure core so
        they're unit tested in ``tests/test_routing.py``.
        """
        decision = decide_inbound(
            recipient_persona=recipient_persona,
            author_account_id=author_account_id,
            own_account_ids=self._own_account_ids,
            config=self.mux,
        )
        if not decision.process:
            logger.debug("[%s] dropping message (%s)", PLATFORM_NAME, decision.reason)
            return None

        source = self.build_source(  # type: ignore[attr-defined]
            chat_id=encode_chat_id(recipient_persona, raw_chat_id),
            **event_fields.get("source_fields", {}),
        )
        event = MessageEvent(
            text=event_fields.get("text", ""),
            message_type=event_fields.get("message_type"),
            source=source,
            message_id=event_fields.get("message_id"),
            metadata={**event_fields.get("metadata", {}), **persona_metadata(recipient_persona)},
        )
        return self.handle_message(event)  # type: ignore[attr-defined]

    # -- outbound ----------------------------------------------------------
    async def send(self, chat_id, content, reply_to=None, metadata=None):  # pragma: no cover
        """Route an outbound message through the correct persona's client."""
        metadata = metadata or {}
        inbound_persona, raw_chat_id = decode_chat_id(str(chat_id))
        persona_id = resolve_outbound_persona(
            explicit=metadata.get("persona"),
            inbound=inbound_persona,
            config=self.mux,
        )
        client = self._clients.get(persona_id)
        if client is None:
            return SendResult(success=False, message_id=None,
                              error=f"persona '{persona_id}' is not online")
        # TODO(spike): await client.send(raw_chat_id, content, reply_to=reply_to)
        return SendResult(success=True, message_id=None)

    async def get_chat_info(self, chat_id):  # pragma: no cover - needs live env
        _, raw = decode_chat_id(str(chat_id))
        return {"name": raw, "type": "dm"}


# -- lock primitives (real ones resolved lazily from Hermes) ---------------
def _real_acquire_lock(platform: str, token: str) -> bool:  # pragma: no cover
    from gateway.status import acquire_scoped_lock

    return bool(acquire_scoped_lock(platform, token))


def _real_release_lock(platform: str, token: str) -> None:  # pragma: no cover
    from gateway.status import release_scoped_lock

    release_scoped_lock(platform, token)


# -- standalone (out-of-process) cron sender -------------------------------
async def standalone_send(pconfig, chat_id, message, *, thread_id=None,
                          media_files=None, force_document=False):  # pragma: no cover
    """Cron delivery when the job runs outside the gateway process.

    TODO(spike): open an ephemeral discord client for the resolved persona's
    token, send, and close. Mirrors the pattern documented in Hermes'
    "Adding a Platform Adapter" guide (``standalone_sender_fn``).
    """
    raise NotImplementedError("standalone_send is wired during the spike")


def register(ctx):
    """Hermes plugin entry point — registers the multiplexer platform.

    Kept free of Hermes imports so it is unit-testable with a fake ``ctx``.
    """
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Discord Personas",
        adapter_factory=lambda cfg: DiscordPersonasAdapter(cfg),
        check_fn=lambda: _HERMES_AVAILABLE,
        platform_hint=(
            "You are one shared brain speaking through multiple Discord bot "
            "identities. The 'persona' field on the incoming message names which "
            "identity you are right now — answer in that persona's voice. Never "
            "@-mention your own personas (it would loop)."
        ),
        emoji="🎭",
        cron_deliver_env_var="DISCORD_PERSONAS_HOME_CHANNEL",
        standalone_sender_fn=standalone_send,
        allowed_users_env="DISCORD_PERSONAS_ALLOWED_USERS",
        allow_all_env="DISCORD_PERSONAS_ALLOW_ALL_USERS",
        max_message_length=2000,
    )
    return PLATFORM_NAME
