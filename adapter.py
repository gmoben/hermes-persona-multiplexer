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
    extract_next_persona,
    parse_config,
    resolve_outbound_persona,
    split_reply_persona,
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
        cache_image_from_bytes,
        cache_image_from_url,
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
    """Per-message system prompt telling the brain which face it wears now (DMs)."""
    return (
        f"You are replying as the **{label}** persona (id `{persona_id}`) — one of "
        f"several Discord faces sharing this single brain and memory. Speak only in "
        f"{label}'s voice and stay in their lane. Never @-mention or impersonate the "
        f"other personas (it causes loops)."
    )


def shared_channel_prompt(personas, orchestrator_id: str) -> str:
    """Per-message system prompt for the shared channel — fast ack, then route the reply.

    All personas live in the shared channel but the message reaches the brain once
    (via the orchestrator). For snappy UX the brain first sends a one-line ack as the
    orchestrator carrying a ``[next:<id>]`` hint (which moves the typing indicator to
    the answering persona without changing who sends the ack), then does the work and
    sends its full answer starting with a ``[persona:<id>]`` tag, which the send path
    consumes to deliver through the chosen persona's bot account.
    """
    roster = ", ".join(
        f"`{p.id}`" + (f" ({p.display_name})" if p.display_name else "") for p in personas
    )
    return (
        "This is the shared channel where all your personas live — one brain, many "
        "Discord faces. Respond in two steps:\n"
        "1. Right away, send ONE short orchestrator line that begins with a `[next:<id>]` "
        "tag naming who will answer, then a brief note of what you're doing — e.g. "
        '"[next:<id>] On it — looking into that now…". The `[next:]` tag only moves the '
        "typing indicator to that persona; the line itself is still sent by you, the "
        "orchestrator.\n"
        "2. Then do the work and send your full answer, starting it with a `[persona:<id>]` "
        "tag naming the single persona who should respond.\n"
        "Personas: " + roster + f". Use the orchestrator `{orchestrator_id}` for general, "
        "multi-topic, or coordination messages. Speak only in the chosen persona's voice, "
        "and never @-mention the other bots (it loops)."
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
        self._account_to_persona: dict[str, str] = {}      # bot user id -> persona id
        self._tokens: dict[str, str] = {}                  # persona_id -> token value
        self._typing_tasks: dict[str, asyncio.Task] = {}   # chat_id -> typing-loop task
        self._typing_persona: dict[str, str] = {}          # chat_id -> persona currently typing
        # Shared-channel only: the persona slated to deliver the reply, set from a
        # `[next:<id>]` ack hint or a `[persona:<id>]` reply so typing follows them.
        self._reply_persona: dict[str, str] = {}           # raw_chat_id -> persona
        # Shared home channel (cron + content-led inbound); None disables channel intake.
        self._home_channel_id: str | None = (
            os.getenv("DISCORD_PERSONAS_HOME_CHANNEL", "").strip() or None
        )

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
                adapter._account_to_persona[str(client.user.id)] = pid
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
            # Make mentions of our own personas readable to the brain
            # (`<@123>` -> `@Label`) so it can honor explicit address in-channel.
            text = message.content or ""
            for um in getattr(message, "mentions", None) or []:
                mapped = adapter._account_to_persona.get(str(getattr(um, "id", "")))
                if mapped:
                    label = (
                        adapter.mux.persona(mapped).label if adapter.mux.has(mapped) else mapped
                    )
                    text = text.replace(f"<@{um.id}>", f"@{label}").replace(
                        f"<@!{um.id}>", f"@{label}"
                    )
            # Cache image attachments to local paths so the brain can see them
            # (vision). Without this, photos (e.g. a meal snapshot) are dropped.
            media_urls: list[str] = []
            media_types: list[str] = []
            for att in getattr(message, "attachments", None) or []:
                ctype = getattr(att, "content_type", None) or ""
                if not ctype.startswith("image/"):
                    continue
                ext = "." + ctype.split("/")[-1].split(";")[0]
                if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                    ext = ".jpg"
                try:
                    media_urls.append(await adapter._cache_image(att, ext))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[%s] image cache failed (%s); using URL", PLATFORM_NAME, exc)
                    media_urls.append(att.url)
                media_types.append(ctype)
            await adapter._on_inbound(
                recipient_persona=pid,
                author_account_id=str(message.author.id),
                raw_chat_id=str(message.channel.id),
                text=text,
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
                media_urls=media_urls,
                media_types=media_types,
            )

        return client

    async def _cache_image(self, att, ext: str) -> str:  # pragma: no cover - needs live env
        """Download an image attachment to the local cache; return its path.

        Mirrors the bundled Discord adapter: prefer the raw bytes (att.read), fall
        back to the CDN URL (SSRF-gated by Hermes's cache_image_from_url).
        """
        try:
            raw = await att.read()
            return cache_image_from_bytes(raw, ext=ext)
        except Exception:  # noqa: BLE001
            return await cache_image_from_url(att.url, ext=ext)

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
        self._account_to_persona.clear()
        self._reply_persona.clear()
        self._typing_persona.clear()
        self._mark_disconnected()

    # -- inbound -----------------------------------------------------------
    async def _on_inbound(self, *, recipient_persona: str, author_account_id: str | None,
                          raw_chat_id: str, text: str, message_id: str | None,
                          chat_type: str, chat_name: str | None, user_name: str | None,
                          guild_id: str | None = None, is_bot: bool = False,
                          media_urls: list[str] | None = None,
                          media_types: list[str] | None = None):
        """Normalize a Discord message into a persona-tagged ``MessageEvent``.

        Loop prevention + persona resolution live in the pure core
        (``tests/test_routing.py``); this just wires Discord -> Hermes.
        """
        is_dm = chat_type == "dm"
        decision = decide_inbound(
            recipient_persona=recipient_persona,
            author_account_id=author_account_id,
            own_account_ids=self._own_account_ids,
            config=self.mux,
            is_dm=is_dm,
            channel_id=raw_chat_id,
            home_channel_id=self._home_channel_id,
        )
        if not decision.process:
            logger.debug("[%s] dropping message (%s)", PLATFORM_NAME, decision.reason)
            return None

        if not is_dm:
            # New shared-channel turn: type as the orchestrator (who sends the ack)
            # until the ack's `[next:]` hint names the answering persona.
            self._reply_persona.pop(raw_chat_id, None)

        persona = self.mux.persona(recipient_persona)
        # DMs are owned by the addressed persona; shared-channel messages are
        # intaken by the orchestrator, which classifies + routes the reply.
        channel_prompt = (
            persona_channel_prompt(persona.label, recipient_persona)
            if is_dm
            else shared_channel_prompt(self.mux.personas, self.mux.orchestrator)
        )
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
        media_urls = media_urls or []
        media_types = media_types or []
        msg_type = MessageType.TEXT
        if media_urls:
            msg_type = getattr(MessageType, "PHOTO", MessageType.TEXT)
        event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            message_id=message_id,
            channel_prompt=channel_prompt,
            media_urls=media_urls,
            media_types=media_types,
        )
        return await self.handle_message(event)  # type: ignore[attr-defined]

    # -- outbound ----------------------------------------------------------
    async def send(self, chat_id, content, reply_to=None, metadata=None):  # pragma: no cover
        """Route an outbound message through the correct persona's client.

        Handles three leading/embedded tags the brain may emit:
        - `[next:<id>]` (leading) — typing hint; doesn't change who sends this message.
        - `[persona:<id>]` — which persona delivers the reply. It may appear mid-message
          when the brain bundles an orchestrator ack and the reply together; we split at
          it so the ack goes out as the orchestrator and the reply as the persona, and
          the tag never leaks to the user.
        """
        metadata = metadata or {}
        inbound_persona, raw_chat_id = decode_chat_id(str(chat_id))
        content = str(content)

        next_persona, content = extract_next_persona(content, self.mux.ids)
        if next_persona:
            self._reply_persona[raw_chat_id] = next_persona
            await self.send_typing(str(chat_id))

        try:
            inbound_target = resolve_outbound_persona(
                explicit=metadata.get("persona"), inbound=inbound_persona, config=self.mux)
        except KeyError as exc:
            return SendResult(success=False, error=str(exc))

        reply_persona, before, after = split_reply_persona(content, self.mux.ids)
        if reply_persona:
            self._reply_persona[raw_chat_id] = reply_persona
            # Text before the tag (e.g. a bundled orchestrator ack) is delivered by the
            # inbound/orchestrator persona; the reply itself by the tagged persona.
            if before.strip():
                await self._deliver(raw_chat_id, before, inbound_target)
            return await self._deliver(raw_chat_id, after, reply_persona)

        return await self._deliver(raw_chat_id, content, inbound_target)

    async def _deliver(self, raw_chat_id, content, persona_id):  # pragma: no cover - needs live env
        """Send already-resolved content to a chat through a specific persona's client."""
        if not str(content).strip():
            return SendResult(success=True, message_id=None, raw_response={"control_only": True})
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

    # -- outbound attachments ----------------------------------------------
    def _persona_for_outbound(self, chat_id):
        """Resolve ``(persona_id, raw_chat_id)`` for an outbound action on a chat.

        In the shared home channel the *answering* persona owns the reply and any
        attachments that trail it (tracked in ``_reply_persona`` from the turn's
        ``[next:]``/``[persona:]`` tag); a DM is owned by its addressed persona.
        Falls back to the default persona when the resolved id isn't configured.
        """
        persona_id, raw_chat_id = decode_chat_id(str(chat_id))
        if self._home_channel_id and raw_chat_id == self._home_channel_id:
            persona_id = self._reply_persona.get(raw_chat_id, persona_id)
        if persona_id is None or not self.mux.has(persona_id):
            persona_id = self.mux.default_persona
        return persona_id, raw_chat_id

    async def _send_attachment(self, chat_id, file_path, caption=None,
                               file_name=None):  # pragma: no cover - needs live env
        """Upload a local file as a native Discord attachment via the right persona.

        The brain emits ``MEDIA:<path>`` (or a bare artifact path) in its reply;
        Hermes extracts + security-validates those and dispatches them to the
        ``send_*`` methods below, so a file the agent produced — a CSV export, a
        chart, a PDF — arrives as a real downloadable attachment instead of an
        undownloadable container path. The file is sent by the same persona that
        delivered the reply (see :meth:`_persona_for_outbound`).
        """
        persona_id, raw_chat_id = self._persona_for_outbound(chat_id)
        client = self._clients.get(persona_id)
        if client is None:
            return SendResult(success=False, error=f"persona '{persona_id}' is not online")
        try:
            channel = client.get_channel(int(raw_chat_id))
            if channel is None:
                channel = await client.fetch_channel(int(raw_chat_id))
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"channel {raw_chat_id} unreachable: {exc}")
        caption = (caption or "").strip() or None
        try:
            filename = file_name or os.path.basename(file_path)
            with open(file_path, "rb") as fh:
                msg = await channel.send(content=caption, file=discord.File(fh, filename=filename))
        except FileNotFoundError:
            return SendResult(success=False, error=f"file not found: {file_path}")
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] attachment send failed for persona '%s': %s",
                         PLATFORM_NAME, persona_id, exc)
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, message_id=str(msg.id),
                          raw_response={"message_id": str(msg.id), "persona": persona_id})

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None):  # pragma: no cover - needs live env
        """Deliver an arbitrary file (CSV, PDF, …) as a native attachment."""
        return await self._send_attachment(chat_id, file_path, caption, file_name)

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None,
                              metadata=None, **kwargs):  # pragma: no cover - needs live env
        """Deliver a local image file; Discord renders attachments inline."""
        return await self._send_attachment(chat_id, image_path, caption)

    async def send_video(self, chat_id, video_path, caption=None, reply_to=None,
                         metadata=None, **kwargs):  # pragma: no cover - needs live env
        """Deliver a local video as a native, inline-playable attachment."""
        return await self._send_attachment(chat_id, video_path, caption)

    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None,
                        metadata=None, **kwargs):  # pragma: no cover - needs live env
        """Deliver an audio file as an attachment (bot accounts can't post voice notes)."""
        return await self._send_attachment(chat_id, audio_path, caption)

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
        # Shared channel: type as the persona slated to reply once known (set from a
        # `[next:]` ack hint or a `[persona:]` reply); until then the orchestrator —
        # who sends the ack — is the typer. DMs always type as the addressed persona.
        persona_id, raw_chat_id = self._persona_for_outbound(key)
        # Already showing this persona's indicator for this chat — nothing to do.
        if self._typing_persona.get(key) == persona_id and key in self._typing_tasks:
            return
        # First time, or the answering persona changed → (re)start on the new client.
        if key in self._typing_tasks:
            await self.stop_typing(key)
        client = self._clients.get(persona_id)
        if client is None:
            return
        self._typing_persona[key] = persona_id

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
                self._typing_persona.pop(key, None)

        self._typing_tasks[key] = asyncio.create_task(_typing_loop())

    async def stop_typing(self, chat_id):  # pragma: no cover - needs live env
        self._typing_persona.pop(str(chat_id), None)
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
    # Let a cron job pick its persona with a leading `[persona:<id>]` tag (e.g. a
    # scheduled report delivers as a specific persona); else fall back to the chat's persona.
    tag_persona, before, after = split_reply_persona(str(message), mux.ids)
    if tag_persona:
        message = (before + after).strip()
    persona_id = resolve_outbound_persona(explicit=tag_persona, inbound=inbound_persona, config=mux)
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
