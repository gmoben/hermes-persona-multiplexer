"""Hermes gateway wiring for the persona multiplexer.

Connects the pure decision logic in :mod:`routing` to the Hermes gateway by
**composing the bundled Discord adapter** rather than re-implementing it. One
real ``DiscordAdapter`` *delegate* runs per persona/bot token, so every native
Discord behavior — inbound attachment caching (images, audio, documents, voice),
history backfill, message chunking/formatting, rate-limit handling, native file
uploads — is inherited for free. This module is a thin **router**:

* one platform (``discord_personas``) is registered with the gateway;
* each delegate's inbound is re-tagged with the persona whose bot account
  received it (persona encoded into the session ``chat_id``, an ephemeral
  ``channel_prompt`` telling the brain which face it wears) and forwarded to the
  single shared-brain agent;
* outbound calls decode the persona from the ``chat_id`` and delegate to that
  persona's adapter, so replies/attachments/typing come from the right face.

The only built-in behavior we override is the delegate's ``on_message`` channel
gating: the bundled adapter routes ``@mentions`` to individual bots, whereas our
orchestrator intakes the shared channel and routes via ``[persona:]`` tags. We
keep the delegate's event-builder (``_handle_message``) so attachment caching and
dedup still apply.

``discord.py``, Hermes, and the bundled Discord adapter imports are all guarded so
this module stays importable (for unit-testing the pure core) without them.
"""

from __future__ import annotations

import asyncio
import logging
import os

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
        MessageEvent,  # noqa: F401  (re-exported for tests/back-compat)
        MessageType,  # noqa: F401
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

try:  # pragma: no cover - bundled Discord adapter is only present in a Hermes install
    from plugins.platforms.discord.adapter import DiscordAdapter

    _DELEGATE_AVAILABLE = True
except Exception:  # noqa: BLE001
    DiscordAdapter = None  # type: ignore[assignment]
    _DELEGATE_AVAILABLE = False


# Delimiter used to namespace a persona into a Discord chat id so the persona
# round-trips through Hermes session keys without a separate sidecar store.
# Each persona DM/channel becomes its own session, but they share the profile's
# memory — one brain, many faces.
PERSONA_PREFIX = "p!"

#: How long to wait for each persona's delegate to reach READY.
READY_TIMEOUT_SECONDS = 30.0

#: Discord hard limit on a single message (used by the standalone cron sender).
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
    """Multiplexing Discord adapter: N bundled ``DiscordAdapter`` delegates → one brain."""

    #: Streaming via progressive ``edit_message`` is intentionally disabled: the
    #: answering persona isn't known until the brain emits its ``[persona:]`` tag,
    #: so we deliver one final tagged message (ack-then-route) rather than stream.
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM_NAME))
        extra = getattr(config, "extra", None) or {}
        self.mux: MultiplexerConfig = parse_config(extra.get(PLATFORM_NAME, extra))
        self._delegates: dict[str, DiscordAdapter] = {}   # persona_id -> bundled adapter
        self._own_account_ids: set[str] = set()             # bot user ids of all personas
        # Shared-channel only: the persona slated to deliver the reply, set from a
        # `[next:<id>]` ack hint or a `[persona:<id>]` reply so typing + attachments follow.
        self._reply_persona: dict[str, str] = {}            # raw_chat_id -> persona
        self._typing_delegate: dict[str, str] = {}          # chat_id -> persona currently typing
        # Shared home channel (cron + content-led inbound); None disables channel intake.
        self._home_channel_id: str | None = (
            os.getenv("DISCORD_PERSONAS_HOME_CHANNEL", "").strip() or None
        )

    # -- lifecycle ---------------------------------------------------------
    def _build_delegate(self, token: str) -> DiscordAdapter:
        """Construct a bundled DiscordAdapter for one persona, configured lean.

        Toggles (via ``PlatformConfig.extra``) keep delegates lightweight and let our
        router own channel routing while still inheriting the native machinery:
          * ``slash_commands`` off — no per-bot slash registration/command-sync churn;
          * ``auto_thread`` off    — we use the shared channel, not auto-threads;
          * ``require_mention`` off — the router (``decide_inbound``) gates the channel;
          * ``history_backfill`` on — inherit native cold-start context backfill.
        """
        extra = {
            "slash_commands": False,
            "auto_thread": False,
            "require_mention": False,
            "history_backfill": True,
        }
        cfg = PlatformConfig(
            enabled=True,
            token=token,
            reply_to_mode=getattr(self.config, "reply_to_mode", "first"),
            extra=extra,
        )
        return DiscordAdapter(cfg)

    async def connect(self) -> bool:  # pragma: no cover - needs live env
        """Start one bundled Discord delegate per persona; degrade per-persona on failure."""
        if not _DISCORD_AVAILABLE:
            logger.error("[%s] discord.py is not installed", PLATFORM_NAME)
            return False
        if not _DELEGATE_AVAILABLE:
            logger.error("[%s] bundled Discord adapter (plugins.platforms.discord) unavailable",
                         PLATFORM_NAME)
            return False

        online: list[str] = []
        degraded: list[str] = []
        for persona in self.mux.personas:
            token = os.getenv(persona.token_env, "").strip()
            if not token:
                logger.warning("[%s] persona '%s' has no token in %s; skipping",
                               PLATFORM_NAME, persona.id, persona.token_env)
                degraded.append(persona.id)
                continue
            delegate = self._build_delegate(token)
            # Route the delegate's inbound through our re-tagger (persona + chat_id).
            delegate.set_message_handler(self._make_inbound_handler(persona.id))
            try:
                ok = await delegate.connect()
            except Exception as exc:  # noqa: BLE001
                logger.error("[%s] persona '%s' connect failed: %s", PLATFORM_NAME, persona.id, exc)
                degraded.append(persona.id)
                continue
            if not ok:
                logger.warning("[%s] persona '%s' delegate did not connect",
                               PLATFORM_NAME, persona.id)
                degraded.append(persona.id)
                continue
            # Wait for READY so the bot's own account id is known (loop prevention).
            try:
                await asyncio.wait_for(delegate._ready_event.wait(), timeout=READY_TIMEOUT_SECONDS)
            except (TimeoutError, AttributeError):
                pass
            # Replace the delegate's @mention channel-gating with our orchestrator intake
            # (reusing its event-builder, so attachment caching + dedup still apply).
            delegate._client.on_message = self._make_on_message(delegate)
            self._delegates[persona.id] = delegate
            client_user = getattr(getattr(delegate, "_client", None), "user", None)
            if client_user is not None:
                self._own_account_ids.add(str(client_user.id))
            online.append(persona.id)

        if not self._delegates:
            logger.error("[%s] no personas could connect", PLATFORM_NAME)
            return False

        self._mark_connected()
        logger.info("[%s] online personas=%s degraded=%s",
                    PLATFORM_NAME, tuple(online), tuple(degraded))
        return True

    def _make_on_message(self, delegate):  # pragma: no cover - needs live env
        """Our minimal inbound gate, replacing the delegate's @mention channel routing.

        We keep the delegate's event-builder (``_handle_message`` — caches attachments,
        builds the ``MessageEvent``) but drop its multi-agent mention filtering so the
        orchestrator can intake every shared-channel message (``decide_inbound`` then
        decides who actually processes it).
        """
        async def _on_message(message):  # noqa: ANN001
            try:
                if delegate._dedup.is_duplicate(str(message.id)):
                    return
                client_user = getattr(delegate._client, "user", None)
                if client_user is not None and message.author == client_user:
                    return
                if getattr(message.author, "bot", False):
                    return  # other bots/personas — loop prevention
                if message.type not in (discord.MessageType.default, discord.MessageType.reply):
                    return
                await delegate._handle_message(message)
            except Exception:  # noqa: BLE001
                logger.exception("[%s] inbound handling failed", PLATFORM_NAME)

        return _on_message

    async def disconnect(self) -> None:  # pragma: no cover - needs live env
        for key in list(self._typing_delegate.keys()):
            await self.stop_typing(key)
        for persona_id, delegate in list(self._delegates.items()):
            try:
                await delegate.disconnect()
            except Exception:  # noqa: BLE001
                logger.debug("[%s] error disconnecting persona '%s'", PLATFORM_NAME, persona_id)
        self._delegates.clear()
        self._own_account_ids.clear()
        self._reply_persona.clear()
        self._typing_delegate.clear()
        self._mark_disconnected()

    # -- inbound -----------------------------------------------------------
    def _make_inbound_handler(self, persona_id: str):
        """Bind a delegate's message handler to our re-tagger for ``persona_id``."""
        async def _handler(event):
            return await self._dispatch_inbound(persona_id, event)

        return _handler

    async def _dispatch_inbound(self, persona_id: str, event):
        """Re-tag a delegate-built event with its persona and forward to the brain.

        Loop prevention + orchestrator/channel routing live in the pure core
        (``tests/test_routing.py``); this wires the delegate's event to Hermes.
        """
        src = event.source
        raw_chat_id = str(getattr(src, "chat_id", "") or "")
        is_dm = getattr(src, "chat_type", None) == "dm"
        decision = decide_inbound(
            recipient_persona=persona_id,
            author_account_id=str(getattr(src, "user_id", "") or "") or None,
            own_account_ids=self._own_account_ids,
            config=self.mux,
            is_dm=is_dm,
            channel_id=raw_chat_id,
            home_channel_id=self._home_channel_id,
        )
        if not decision.process:
            logger.debug("[%s] dropping message (%s) persona=%s",
                         PLATFORM_NAME, decision.reason, persona_id)
            return None

        if not is_dm:
            # New shared-channel turn: typing starts on the orchestrator (who sends the
            # ack) until a `[next:]`/`[persona:]` tag names the answering persona.
            self._reply_persona.pop(raw_chat_id, None)

        # Re-tag: namespace the session to this persona and route replies back through
        # the router (not the delegate), and tell the brain which face it wears.
        src.chat_id = encode_chat_id(persona_id, raw_chat_id)
        src.platform = self.platform
        label = self.mux.persona(persona_id).label
        event.channel_prompt = (
            persona_channel_prompt(label, persona_id)
            if is_dm
            else shared_channel_prompt(self.mux.personas, self.mux.orchestrator)
        )
        if self._message_handler is None:
            return None
        return await self._message_handler(event)

    # -- outbound ----------------------------------------------------------
    def _persona_for_outbound(self, chat_id):
        """Resolve ``(persona_id, raw_chat_id)`` for an outbound action on a chat.

        In the shared home channel the *answering* persona owns the reply and any
        attachments/typing that trail it (tracked in ``_reply_persona`` from the turn's
        ``[next:]``/``[persona:]`` tag); a DM is owned by its addressed persona.
        Falls back to the default persona when the resolved id isn't configured.
        """
        persona_id, raw_chat_id = decode_chat_id(str(chat_id))
        if self._home_channel_id and raw_chat_id == self._home_channel_id:
            persona_id = self._reply_persona.get(raw_chat_id, persona_id)
        if persona_id is None or not self.mux.has(persona_id):
            persona_id = self.mux.default_persona
        return persona_id, raw_chat_id

    def _delegate_for(self, chat_id):
        """Return ``(delegate, raw_chat_id, persona_id)`` for an outbound chat id."""
        persona_id, raw_chat_id = self._persona_for_outbound(chat_id)
        return self._delegates.get(persona_id), raw_chat_id, persona_id

    async def send(self, chat_id, content, reply_to=None, metadata=None):  # pragma: no cover
        """Route an outbound message through the correct persona's delegate.

        Handles the brain's leading/embedded tags:
        - `[next:<id>]` (leading) — typing hint; doesn't change who sends this message.
        - `[persona:<id>]` — which persona delivers the reply. It may appear mid-message
          when the brain bundles an orchestrator ack and the reply together; we split at
          it so the ack goes out as the orchestrator and the reply as the persona.
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
            if before.strip():
                await self._deliver(raw_chat_id, before, inbound_target, reply_to, metadata)
            return await self._deliver(raw_chat_id, after, reply_persona, reply_to, metadata)

        return await self._deliver(raw_chat_id, content, inbound_target, reply_to, metadata)

    async def _deliver(self, raw_chat_id, content, persona_id, reply_to=None,
                       metadata=None):  # pragma: no cover - needs live env
        """Send already-resolved text to a chat through a specific persona's delegate."""
        if not str(content).strip():
            return SendResult(success=True, message_id=None, raw_response={"control_only": True})
        delegate = self._delegates.get(persona_id)
        if delegate is None:
            return SendResult(success=False, message_id=None,
                              error=f"persona '{persona_id}' is not online")
        # The delegate handles native formatting, chunking, reply-to, and rate limits.
        return await delegate.send(raw_chat_id, content, reply_to=reply_to, metadata=metadata)

    # Native attachment + media delivery — routed to the answering persona's delegate,
    # which uploads real discord.File attachments (CSV/PDF/image/video/audio).
    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None):  # pragma: no cover - needs live env
        d, raw, pid = self._delegate_for(chat_id)
        if d is None:
            return SendResult(success=False, error=f"persona '{pid}' is not online")
        return await d.send_document(raw, file_path, caption=caption, file_name=file_name,
                                     reply_to=reply_to, metadata=metadata)

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None,
                              metadata=None, **kwargs):  # pragma: no cover - needs live env
        d, raw, pid = self._delegate_for(chat_id)
        if d is None:
            return SendResult(success=False, error=f"persona '{pid}' is not online")
        return await d.send_image_file(raw, image_path, caption=caption, reply_to=reply_to,
                                       metadata=metadata, **kwargs)

    async def send_image(self, chat_id, image_url, caption=None, reply_to=None,
                         metadata=None):  # pragma: no cover - needs live env
        d, raw, pid = self._delegate_for(chat_id)
        if d is None:
            return SendResult(success=False, error=f"persona '{pid}' is not online")
        return await d.send_image(raw, image_url, caption=caption, reply_to=reply_to,
                                  metadata=metadata)

    async def send_multiple_images(self, chat_id, images, metadata=None,
                                   human_delay=0.0):  # pragma: no cover - needs live env
        d, raw, _pid = self._delegate_for(chat_id)
        if d is None:
            return None
        return await d.send_multiple_images(raw, images, metadata=metadata, human_delay=human_delay)

    async def send_video(self, chat_id, video_path, caption=None, reply_to=None,
                         metadata=None, **kwargs):  # pragma: no cover - needs live env
        d, raw, pid = self._delegate_for(chat_id)
        if d is None:
            return SendResult(success=False, error=f"persona '{pid}' is not online")
        return await d.send_video(raw, video_path, caption=caption, reply_to=reply_to,
                                  metadata=metadata, **kwargs)

    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None,
                        metadata=None, **kwargs):  # pragma: no cover - needs live env
        d, raw, pid = self._delegate_for(chat_id)
        if d is None:
            return SendResult(success=False, error=f"persona '{pid}' is not online")
        return await d.send_voice(raw, audio_path, caption=caption, reply_to=reply_to,
                                  metadata=metadata, **kwargs)

    async def get_chat_info(self, chat_id):  # pragma: no cover - needs live env
        d, raw, _pid = self._delegate_for(chat_id)
        if d is None:
            return {"name": raw, "type": "dm"}
        return await d.get_chat_info(raw)

    # -- typing indicator --------------------------------------------------
    async def send_typing(self, chat_id, metadata=None):  # pragma: no cover - needs live env
        """Show 'typing…' as the persona slated to answer (delegate-native typing loop).

        In the shared channel the answering persona is set from a `[next:]`/`[persona:]`
        tag; until then it's the orchestrator. When it switches mid-turn we stop the
        previous persona's indicator and start the new one. DMs always type as the
        addressed persona.
        """
        key = str(chat_id)
        persona_id, raw_chat_id = self._persona_for_outbound(key)
        if self._typing_delegate.get(key) == persona_id:
            return  # already typing as this persona for this chat
        prev = self._typing_delegate.get(key)
        if prev and prev in self._delegates:
            await self._delegates[prev].stop_typing(raw_chat_id)
        delegate = self._delegates.get(persona_id)
        if delegate is None:
            return
        self._typing_delegate[key] = persona_id
        await delegate.send_typing(raw_chat_id, metadata)

    async def stop_typing(self, chat_id):  # pragma: no cover - needs live env
        key = str(chat_id)
        persona_id = self._typing_delegate.pop(key, None)
        _, raw_chat_id = decode_chat_id(key)
        if persona_id and persona_id in self._delegates:
            await self._delegates[persona_id].stop_typing(raw_chat_id)


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
        check_fn=lambda: _HERMES_AVAILABLE and _DISCORD_AVAILABLE and _DELEGATE_AVAILABLE,
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
