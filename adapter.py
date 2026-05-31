"""Hermes gateway wiring for the persona multiplexer.

Connects the pure decision logic in :mod:`routing` and :mod:`locks` to the
Hermes gateway and ``discord.py``.

It runs **N Discord clients in one process** (one per persona/bot token), tags
every inbound event with the persona whose account received it, and forwards all
of them to the single gateway runner — i.e. one shared-brain agent. The persona
is conveyed to the agent per-message via ``channel_prompt`` (an ephemeral system
prompt) and the persona is encoded into the session ``chat_id`` so outbound
replies route back through the originating persona's client.

Both ``discord.py`` and Hermes imports are guarded so this module stays
importable (for unit-testing the pure core) on machines without either.
"""

from __future__ import annotations

import asyncio
import logging
import os

from .locks import ScopedLockManager
from .routing import (
    PLATFORM_NAME,
    MultiplexerConfig,
    decide_inbound,
    parse_config,
    resolve_outbound_persona,
)

logger = logging.getLogger(__name__)

try:  # pragma: no cover - discord.py is optional for core tests
    import discord

    _DISCORD_AVAILABLE = True
except Exception:  # noqa: BLE001
    discord = None  # type: ignore[assignment]
    _DISCORD_AVAILABLE = False

try:  # pragma: no cover - exercised only inside a Hermes install
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import (
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )

    _HERMES_AVAILABLE = True
except Exception:  # noqa: BLE001
    _HERMES_AVAILABLE = False

    class BasePlatformAdapter:  # type: ignore[no-redef]
        """Import shim so this module loads without Hermes (tests/CI)."""

        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "persona-multiplexer adapter requires Hermes Agent at runtime "
                "(gateway.platforms.base could not be imported)."
            )


# Delimiter used to namespace a persona into a Discord chat id so the persona
# round-trips through Hermes session keys without a separate sidecar store.
# Each persona DM/channel becomes its own session, but they share the profile's
# memory — one brain, many faces.
PERSONA_PREFIX = "p!"

#: How long to wait for each persona's Discord client to reach READY.
READY_TIMEOUT_SECONDS = 30.0

#: Discord hard limit on a single message.
MAX_MESSAGE_LENGTH = 2000


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


def persona_channel_prompt(label: str, persona_id: str) -> str:
    """Per-message system prompt telling the brain which face it wears now."""
    return (
        f"You are replying as the **{label}** persona (id `{persona_id}`) of the "
        f"Mushroom Kingdom health crew — one shared brain, many Discord faces. "
        f"Speak only in {label}'s voice and stay in their lane. Never @-mention or "
        f"impersonate the other personas (it causes loops)."
    )


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
        self._clients: dict[str, discord.Client] = {}   # persona_id -> client
        self._tasks: dict[str, asyncio.Task] = {}          # persona_id -> client.start() task
        self._ready: dict[str, asyncio.Event] = {}         # persona_id -> READY event
        self._own_account_ids: set[str] = set()            # bot user ids of all personas
        self._tokens: dict[str, str] = {}                  # persona_id -> token value
        self._typing_tasks: dict[str, asyncio.Task] = {}   # chat_id -> typing-loop task

    # -- lifecycle ---------------------------------------------------------
    def _build_client(self, persona) -> discord.Client:
        """Create + wire one persona's Discord client.

        Defined as a method (not an inline loop body) so ``persona`` is bound
        per client and the event closures don't all capture the last persona.
        """
        intents = discord.Intents.default()
        intents.message_content = True   # required to read DM/message text
        intents.dm_messages = True
        intents.guild_messages = True
        client = discord.Client(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),  # never ping @everyone/roles
        )
        ready = self._ready[persona.id]
        adapter = self
        pid = persona.id

        @client.event
        async def on_ready():  # noqa: ANN202 - discord callback
            if client.user is not None:
                adapter._own_account_ids.add(str(client.user.id))
            logger.info("[%s] persona '%s' ready as %s", PLATFORM_NAME, pid, client.user)
            ready.set()

        @client.event
        async def on_message(message):  # noqa: ANN001, ANN202 - discord callback
            # Loop guard #1: our own / sibling persona bots (decide_inbound
            # re-checks via own_account_ids once all personas are READY).
            if client.user is not None and message.author.id == client.user.id:
                return
            if str(message.author.id) in adapter._own_account_ids:
                return
            # Only ordinary text + replies (skip system messages, joins, pins…).
            if getattr(message, "type", None) not in (
                discord.MessageType.default,
                discord.MessageType.reply,
            ):
                return
            is_dm = isinstance(message.channel, discord.DMChannel)
            guild = getattr(message, "guild", None)
            await adapter._on_inbound(
                recipient_persona=pid,
                author_account_id=str(message.author.id),
                raw_chat_id=str(message.channel.id),
                text=message.content or "",
                message_id=str(message.id),
                chat_type="dm" if is_dm else "group",
                chat_name=(
                    message.author.name
                    if is_dm
                    else getattr(message.channel, "name", str(message.channel.id))
                ),
                user_name=getattr(message.author, "display_name", None)
                or getattr(message.author, "name", None),
                guild_id=str(guild.id) if guild else None,
                is_bot=bool(getattr(message.author, "bot", False)),
            )

        return client

    async def connect(self) -> bool:  # pragma: no cover - needs live env
        """Start one Discord client per persona; degrade per-persona on failure."""
        if not _DISCORD_AVAILABLE:
            logger.error("[%s] discord.py is not installed", PLATFORM_NAME)
            return False

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
            self._ready[persona.id] = asyncio.Event()
            client = self._build_client(persona)
            self._clients[persona.id] = client
            self._tasks[persona.id] = asyncio.create_task(client.start(token))

        if not self._clients:
            logger.error("[%s] no personas could start", PLATFORM_NAME)
            return False

        # Wait for each client's READY; personas that don't connect are degraded.
        online: list[str] = []
        degraded: list[str] = []
        for pid in list(self._clients.keys()):
            try:
                await asyncio.wait_for(self._ready[pid].wait(), timeout=READY_TIMEOUT_SECONDS)
                online.append(pid)
            except TimeoutError:
                degraded.append(pid)
                err = self._task_error(pid)
                logger.error("[%s] persona '%s' failed to reach READY in %.0fs%s",
                             PLATFORM_NAME, pid, READY_TIMEOUT_SECONDS,
                             f": {err}" if err else "")
                await self._close_persona(pid)

        if not online:
            logger.error("[%s] no personas reached READY", PLATFORM_NAME)
            return False

        self._mark_connected()
        logger.info("[%s] online personas=%s degraded=%s",
                    PLATFORM_NAME, tuple(online), tuple(degraded))
        return True

    def _task_error(self, persona_id: str) -> str | None:
        """Surface a client.start() failure (e.g. LoginFailure on a bad token)."""
        task = self._tasks.get(persona_id)
        if task is not None and task.done():
            try:
                exc = task.exception()
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                return None
            if exc is not None:
                return f"{type(exc).__name__}: {exc}"
        return None

    async def _close_persona(self, persona_id: str) -> None:  # pragma: no cover - needs live env
        client = self._clients.pop(persona_id, None)
        task = self._tasks.pop(persona_id, None)
        self._ready.pop(persona_id, None)
        if client is not None:
            try:
                if not client.is_closed():
                    await client.close()
            except Exception:  # noqa: BLE001
                logger.debug("[%s] error closing persona '%s'", PLATFORM_NAME, persona_id)
        if task is not None and not task.done():
            task.cancel()

    async def disconnect(self) -> None:  # pragma: no cover - needs live env
        for chat_id in list(self._typing_tasks.keys()):
            await self.stop_typing(chat_id)
        for persona_id in list(self._clients.keys()):
            await self._close_persona(persona_id)
        self.locks.release_all()
        self._own_account_ids.clear()
        self._mark_disconnected()

    # -- inbound -----------------------------------------------------------
    async def _on_inbound(self, *, recipient_persona: str, author_account_id: str | None,
                          raw_chat_id: str, text: str, message_id: str | None,
                          chat_type: str, chat_name: str | None, user_name: str | None,
                          guild_id: str | None = None, is_bot: bool = False):
        """Normalize a Discord message into a persona-tagged ``MessageEvent``.

        Loop prevention + persona resolution live in the pure core
        (``tests/test_routing.py``); this just wires Discord -> Hermes.
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

        persona = self.mux.persona(recipient_persona)
        source = self.build_source(  # type: ignore[attr-defined]
            chat_id=encode_chat_id(recipient_persona, raw_chat_id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=author_account_id,
            user_name=user_name,
            is_bot=is_bot,
            guild_id=guild_id,
            message_id=message_id,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=message_id,
            channel_prompt=persona_channel_prompt(persona.label, recipient_persona),
        )
        return await self.handle_message(event)  # type: ignore[attr-defined]

    # -- outbound ----------------------------------------------------------
    async def send(self, chat_id, content, reply_to=None, metadata=None):  # pragma: no cover
        """Route an outbound message through the correct persona's client."""
        metadata = metadata or {}
        inbound_persona, raw_chat_id = decode_chat_id(str(chat_id))
        try:
            persona_id = resolve_outbound_persona(
                explicit=metadata.get("persona"),
                inbound=inbound_persona,
                config=self.mux,
            )
        except KeyError as exc:
            return SendResult(success=False, error=str(exc))

        client = self._clients.get(persona_id)
        if client is None:
            return SendResult(success=False, message_id=None,
                              error=f"persona '{persona_id}' is not online")

        try:
            channel = client.get_channel(int(raw_chat_id))
            if channel is None:
                channel = await client.fetch_channel(int(raw_chat_id))
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"channel {raw_chat_id} unreachable: {exc}")

        formatted = self.format_message(content)  # type: ignore[attr-defined]
        chunks = self.truncate_message(formatted, MAX_MESSAGE_LENGTH)  # type: ignore[attr-defined]
        message_ids: list[str] = []
        try:
            for chunk in chunks:
                msg = await channel.send(content=chunk)
                message_ids.append(str(msg.id))
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] send failed for persona '%s': %s", PLATFORM_NAME, persona_id, exc)
            return SendResult(success=False, error=str(exc),
                              raw_response={"sent": message_ids, "persona": persona_id})
        return SendResult(success=True,
                          message_id=message_ids[0] if message_ids else None,
                          raw_response={"message_ids": message_ids, "persona": persona_id})

    async def get_chat_info(self, chat_id):  # pragma: no cover - needs live env
        _, raw = decode_chat_id(str(chat_id))
        return {"name": raw, "type": "dm"}

    # -- typing indicator --------------------------------------------------
    async def send_typing(self, chat_id, metadata=None):  # pragma: no cover - needs live env
        """Start a persistent 'typing…' indicator on the originating persona's client.

        Discord's typing indicator lasts ~10s and is unreliable in DMs, so we
        re-trigger it every 12s until ``stop_typing`` is called (the gateway
        invokes both — start when the agent begins working, stop after the
        reply is sent). Keyed by the persona-namespaced ``chat_id``.
        """
        key = str(chat_id)
        persona_id, raw_chat_id = decode_chat_id(key)
        if persona_id is None or not self.mux.has(persona_id):
            persona_id = self.mux.default_persona
        client = self._clients.get(persona_id)
        if client is None or key in self._typing_tasks:
            return

        async def _typing_loop():
            try:
                while True:
                    try:
                        route = discord.http.Route(
                            "POST", "/channels/{channel_id}/typing", channel_id=raw_chat_id
                        )
                        await client.http.request(route)
                    except asyncio.CancelledError:
                        return
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("[%s] typing failed for %s: %s", PLATFORM_NAME, key, exc)
                        return
                    await asyncio.sleep(12)
            except asyncio.CancelledError:
                pass
            finally:
                self._typing_tasks.pop(key, None)

        self._typing_tasks[key] = asyncio.create_task(_typing_loop())

    async def stop_typing(self, chat_id):  # pragma: no cover - needs live env
        task = self._typing_tasks.pop(str(chat_id), None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


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

    Opens an ephemeral client for the resolved persona's token, sends, closes.
    """
    if not _DISCORD_AVAILABLE:
        raise RuntimeError("discord.py is not installed")

    extra = getattr(pconfig, "extra", None) or {}
    mux = parse_config(extra.get(PLATFORM_NAME, extra))
    inbound_persona, raw_chat_id = decode_chat_id(str(chat_id))
    persona_id = resolve_outbound_persona(explicit=None, inbound=inbound_persona, config=mux)
    persona = mux.persona(persona_id)
    token = os.getenv(persona.token_env, "").strip()
    if not token:
        raise RuntimeError(f"persona '{persona_id}' has no token in {persona.token_env}")

    intents = discord.Intents.default()
    client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
    result: dict[str, str | None] = {"id": None}

    @client.event
    async def on_ready():  # noqa: ANN202
        try:
            channel = client.get_channel(int(raw_chat_id))
            if channel is None:
                channel = await client.fetch_channel(int(raw_chat_id))
            msg = await channel.send(content=str(message)[:MAX_MESSAGE_LENGTH])
            result["id"] = str(msg.id)
        finally:
            await client.close()

    await client.start(token)
    return result["id"]


def register(ctx):
    """Hermes plugin entry point — registers the multiplexer platform.

    Kept free of Hermes imports so it is unit-testable with a fake ``ctx``.
    """
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Discord Personas",
        adapter_factory=lambda cfg: DiscordPersonasAdapter(cfg),
        check_fn=lambda: _HERMES_AVAILABLE and _DISCORD_AVAILABLE,
        platform_hint=(
            "You speak through multiple Discord bot identities that share this one "
            "brain and memory. Each incoming message's channel prompt names which "
            "persona you are right now — reply only in that persona's voice, and "
            "never @-mention your other personas (it would loop)."
        ),
        emoji="🎭",
        cron_deliver_env_var="DISCORD_PERSONAS_HOME_CHANNEL",
        standalone_sender_fn=standalone_send,
        allowed_users_env="DISCORD_PERSONAS_ALLOWED_USERS",
        allow_all_env="DISCORD_PERSONAS_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
    )
    return PLATFORM_NAME
