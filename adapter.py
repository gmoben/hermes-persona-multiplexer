"""Hermes gateway wiring for the persona multiplexer.

Connects the pure decision logic in :mod:`routing` to the Hermes gateway by
**composing the bundled Discord adapter** rather than re-implementing it. One
real ``DiscordAdapter`` *delegate* runs per persona/bot token, so every native
Discord behavior — inbound attachment caching (images, audio, documents, voice),
history backfill, message chunking/formatting, rate-limit handling, native file
uploads, typing — is inherited for free. This module is a thin **router**.

The key constraint is that a Hermes adapter *owns sending its own reply*:
``delegate.handle_message()`` builds the session key, asks our handler for the
response, then sends it itself via ``delegate.send(event.source.chat_id)``. So we
do **not** rewrite the chat id; instead:

* each delegate's inbound is gated + given a persona ``channel_prompt`` (re-tag),
  then handed to the one shared-brain agent;
* we alias each delegate's ``platform`` to this router's (``discord_personas``)
  so the gateway routes *gateway-initiated* outbound (media, typing, cron) to the
  router, which forwards to the right persona's delegate;
* we wrap each delegate's ``send`` so the brain's ``[persona:]``/``[next:]`` tags
  route the reply (and the orchestrator's bundled ack) to the answering persona's
  delegate — the delegate then delivers it natively.

The only built-in behavior we replace is the delegate's ``on_message`` channel
gating (bundled @mention routing → our orchestrator intake), reusing its
event-builder ``_handle_message`` so attachment caching + dedup still apply.

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
    is_allowed_user,
    parse_allowed_users,
    parse_config,
    should_intake_shared_channel,
    split_reply_persona,
    tidy_outbound_text,
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
        MessageEvent,  # noqa: F401  (re-exported for back-compat)
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

    class SendResult:  # type: ignore[no-redef]
        """Minimal stand-in so the module imports without Hermes (tests/CI)."""

        def __init__(self, success=True, message_id=None, error=None, raw_response=None):
            self.success = success
            self.message_id = message_id
            self.error = error
            self.raw_response = raw_response

try:  # pragma: no cover - bundled Discord adapter is only present in a Hermes install
    from plugins.platforms.discord.adapter import DiscordAdapter

    _DELEGATE_AVAILABLE = True
except Exception:  # noqa: BLE001
    DiscordAdapter = None  # type: ignore[assignment]
    _DELEGATE_AVAILABLE = False


# Delimiter used to namespace a persona into a chat id. Inbound replies are sent
# by the delegate itself with the raw chat id, so we no longer encode inbound
# events — but cron ``--deliver`` targets and tests still use this form, and the
# router decodes it to pick a persona.
PERSONA_PREFIX = "p!"

#: How long to wait for each persona's delegate to reach READY.
READY_TIMEOUT_SECONDS = 30.0

#: Discord hard limit on a single message (used by the standalone cron sender).
MAX_MESSAGE_LENGTH = 2000

#: Delegate send/media/typing methods the router captures (native) + re-routes.
_ROUTED_METHODS = (
    "send", "send_document", "send_image_file", "send_image",
    "send_multiple_images", "send_video", "send_voice", "send_typing", "stop_typing",
)


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
        self._orig: dict[str, dict] = {}                   # persona_id -> {method: native fn}
        self._own_account_ids: set[str] = set()            # bot user ids of all personas
        # Which persona owns a chat: DMs are owned by the addressed persona (set on
        # inbound); the shared channel's *answering* persona is set from a turn's
        # `[next:]`/`[persona:]` tag (so media + typing follow the reply).
        self._chat_persona: dict[str, str] = {}             # raw_chat_id -> owning persona (DM)
        self._reply_persona: dict[str, str] = {}            # raw_chat_id -> answering persona
        self._typing_delegate: dict[str, str] = {}          # chat_id -> persona currently typing
        # Shared home channel (cron + content-led inbound); None disables channel intake.
        self._home_channel_id: str | None = (
            os.getenv("DISCORD_PERSONAS_HOME_CHANNEL", "").strip() or None
        )
        # Access control: only these user ids may interact with the crew (DMs and the
        # shared channel). Empty = open (restriction is opt-in). Set
        # DISCORD_PERSONAS_ALLOWED_USERS="<id>,<id>" to lock it down.
        self._allowed_users = parse_allowed_users(os.getenv("DISCORD_PERSONAS_ALLOWED_USERS"))

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
            # Gateway-initiated outbound (media, typing, cron) is keyed by platform;
            # alias the delegate onto the router so it routes here, and so session
            # keys are consistent across personas + cron.
            delegate.platform = self.platform
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
            try:
                await asyncio.wait_for(delegate._ready_event.wait(), timeout=READY_TIMEOUT_SECONDS)
            except (TimeoutError, AttributeError):
                pass
            # Capture native methods, then wrap inbound gating + the reply send.
            self._orig[persona.id] = {m: getattr(delegate, m) for m in _ROUTED_METHODS}
            delegate._client.on_message = self._make_on_message(persona.id, delegate)
            delegate.send = self._make_delegate_send(persona.id)
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

    def _make_on_message(self, persona_id, delegate):  # pragma: no cover - needs live env
        """Our minimal inbound gate, replacing the delegate's @mention channel routing.

        We keep the delegate's event-builder (``_handle_message`` — caches attachments,
        builds the ``MessageEvent``, and auto-creates a thread if enabled) but drop its
        multi-agent mention filtering. For a shared channel we pre-gate to the
        orchestrator *before* ``_handle_message`` runs, so only one delegate intakes
        (and only one thread is ever created); the orchestrator then routes the reply
        — into that thread when threading is on — via the ``[persona:]`` tag.
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
                if not is_allowed_user(str(message.author.id), self._allowed_users):
                    return  # access control — drop before any caching/agent work
                if message.type not in (discord.MessageType.default, discord.MessageType.reply):
                    return
                if not isinstance(message.channel, discord.DMChannel):
                    parent_id = getattr(message.channel, "parent_id", None)
                    if not should_intake_shared_channel(
                        persona_id, str(message.channel.id),
                        str(parent_id) if parent_id else None,
                        config=self.mux, home_channel_id=self._home_channel_id,
                    ):
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
        self._orig.clear()
        self._own_account_ids.clear()
        self._chat_persona.clear()
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
        """Gate a delegate-built event + tell the brain which face to wear.

        The delegate sends its own reply (adapter-owns-send), so we do NOT rewrite the
        chat id; we just gate (orchestrator/channel routing lives in the pure core,
        ``tests/test_routing.py``) and stamp the persona ``channel_prompt``. Outbound
        persona routing happens in the wrapped ``send`` + the router's media/typing.
        """
        src = event.source
        raw_chat_id = str(getattr(src, "chat_id", "") or "")
        author_account_id = str(getattr(src, "user_id", "") or "") or None
        if not is_allowed_user(author_account_id, self._allowed_users):
            logger.debug("[%s] dropping message from unauthorized user %s",
                         PLATFORM_NAME, author_account_id)
            return None
        is_dm = getattr(src, "chat_type", None) == "dm"
        # If the delegate auto-threaded, chat_id is the new thread and parent_chat_id is
        # the home channel; gate against the parent so the thread isn't seen as "outside".
        parent_chat_id = str(getattr(src, "parent_chat_id", "") or "")
        effective_channel = parent_chat_id or raw_chat_id
        decision = decide_inbound(
            recipient_persona=persona_id,
            author_account_id=author_account_id,
            own_account_ids=self._own_account_ids,
            config=self.mux,
            is_dm=is_dm,
            channel_id=effective_channel,
            home_channel_id=self._home_channel_id,
        )
        if not decision.process:
            logger.debug("[%s] dropping message (%s) persona=%s",
                         PLATFORM_NAME, decision.reason, persona_id)
            return None

        if is_dm:
            # The addressed persona owns this DM — used to route media/typing back.
            self._chat_persona[raw_chat_id] = persona_id
        else:
            # New shared-channel turn: typing starts on the orchestrator (who sends the
            # ack) until a `[next:]`/`[persona:]` tag names the answering persona.
            self._reply_persona.pop(raw_chat_id, None)

        label = self.mux.persona(persona_id).label
        event.channel_prompt = (
            persona_channel_prompt(label, persona_id)
            if is_dm
            else shared_channel_prompt(self.mux.personas, self.mux.orchestrator)
        )
        if self._message_handler is None:
            return None
        return await self._message_handler(event)

    # -- outbound routing --------------------------------------------------
    def _persona_for_outbound(self, chat_id):
        """Resolve ``(persona_id, raw_chat_id)`` for a gateway-initiated outbound action.

        Priority: an explicit persona encoded in the chat id (cron) → the shared
        channel's current answering persona (``_reply_persona``) → the DM's owning
        persona (``_chat_persona``) → the default persona.
        """
        hint, raw = decode_chat_id(str(chat_id))
        if hint and self.mux.has(hint):
            return hint, raw
        persona = self._reply_persona.get(raw) or self._chat_persona.get(raw)
        if not persona or not self.mux.has(persona):
            persona = self.mux.default_persona
        return persona, raw

    async def _native(self, persona_id, method, *args, **kwargs):  # pragma: no cover - live env
        """Invoke a persona delegate's *native* (un-wrapped) send/media/typing method."""
        methods = self._orig.get(persona_id)
        if not methods or method not in methods:
            return SendResult(success=False, error=f"persona '{persona_id}' is not online")
        return await methods[method](*args, **kwargs)

    async def _route_send(self, chat_id, content, fallback_persona,
                          reply_to=None, metadata=None):  # pragma: no cover - live env
        """Deliver text, honoring the brain's `[next:]`/`[persona:]` tags.

        ``[next:<id>]`` (leading) only switches the typing indicator. ``[persona:<id>]``
        chooses who delivers the reply and may appear mid-message when the brain bundles
        an orchestrator ack with the reply — the ack goes out as ``fallback_persona``
        (the orchestrator), the reply as the tagged persona.
        """
        hint, raw = decode_chat_id(str(chat_id))
        source = hint if (hint and self.mux.has(hint)) else fallback_persona
        content = str(content)

        next_persona, content = extract_next_persona(content, self.mux.ids)
        if next_persona:
            self._reply_persona[raw] = next_persona
            await self.send_typing(raw)

        reply_persona, before, after = split_reply_persona(content, self.mux.ids)
        if reply_persona:
            self._reply_persona[raw] = reply_persona
            if before.strip():
                await self._send_text(source, raw, before, reply_to, metadata)
            return await self._send_text(reply_persona, raw, after, reply_to, metadata)
        return await self._send_text(source, raw, content, reply_to, metadata)

    async def _send_text(self, persona_id, raw_chat_id, content,
                         reply_to=None, metadata=None):  # pragma: no cover - live env
        # Tidy artifacts left by the gateway's MEDIA: stripping (e.g. a dangling
        # "**File:**" label) so they don't render as noise.
        content = tidy_outbound_text(str(content))
        if not content.strip():
            return SendResult(success=True, message_id=None, raw_response={"control_only": True})
        return await self._native(persona_id, "send", raw_chat_id, content,
                                  reply_to=reply_to, metadata=metadata)

    def _make_delegate_send(self, source_pid):  # pragma: no cover - needs live env
        """Wrap a delegate's ``send`` so its own reply honors persona-routing tags.

        The bundled ``handle_message`` delivers the agent's reply via ``self.send`` with
        the raw chat id; we intercept here to split the `[persona:]` tag and route to
        the answering persona's native send.
        """
        async def _send(chat_id, content, reply_to=None, metadata=None):  # noqa: ANN001
            return await self._route_send(chat_id, content, source_pid, reply_to, metadata)

        return _send

    # Gateway-initiated outbound (media, typing, cron text) is dispatched to the
    # router by platform; route each to the answering/owning persona's delegate.
    async def send(self, chat_id, content, reply_to=None, metadata=None):  # pragma: no cover
        return await self._route_send(chat_id, content, self.mux.orchestrator, reply_to, metadata)

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None):  # pragma: no cover - needs live env
        pid, raw = self._persona_for_outbound(chat_id)
        return await self._native(pid, "send_document", raw, file_path, caption=caption,
                                  file_name=file_name, reply_to=reply_to, metadata=metadata)

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None,
                              metadata=None, **kwargs):  # pragma: no cover - needs live env
        pid, raw = self._persona_for_outbound(chat_id)
        return await self._native(pid, "send_image_file", raw, image_path, caption=caption,
                                  reply_to=reply_to, metadata=metadata, **kwargs)

    async def send_image(self, chat_id, image_url, caption=None, reply_to=None,
                         metadata=None):  # pragma: no cover - needs live env
        pid, raw = self._persona_for_outbound(chat_id)
        return await self._native(pid, "send_image", raw, image_url, caption=caption,
                                  reply_to=reply_to, metadata=metadata)

    async def send_multiple_images(self, chat_id, images, metadata=None,
                                   human_delay=0.0):  # pragma: no cover - needs live env
        pid, raw = self._persona_for_outbound(chat_id)
        return await self._native(pid, "send_multiple_images", raw, images,
                                  metadata=metadata, human_delay=human_delay)

    async def send_video(self, chat_id, video_path, caption=None, reply_to=None,
                         metadata=None, **kwargs):  # pragma: no cover - needs live env
        pid, raw = self._persona_for_outbound(chat_id)
        return await self._native(pid, "send_video", raw, video_path, caption=caption,
                                  reply_to=reply_to, metadata=metadata, **kwargs)

    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None,
                        metadata=None, **kwargs):  # pragma: no cover - needs live env
        pid, raw = self._persona_for_outbound(chat_id)
        return await self._native(pid, "send_voice", raw, audio_path, caption=caption,
                                  reply_to=reply_to, metadata=metadata, **kwargs)

    async def get_chat_info(self, chat_id):  # pragma: no cover - needs live env
        pid, raw = self._persona_for_outbound(chat_id)
        methods = self._orig.get(pid)
        if not methods:
            return {"name": raw, "type": "dm"}
        delegate = self._delegates.get(pid)
        return await delegate.get_chat_info(raw)

    # -- typing indicator (follows the answering persona) ------------------
    async def send_typing(self, chat_id, metadata=None):  # pragma: no cover - needs live env
        pid, raw = self._persona_for_outbound(chat_id)
        key = str(raw)
        if self._typing_delegate.get(key) == pid:
            return
        prev = self._typing_delegate.get(key)
        if prev and prev in self._orig:
            await self._native(prev, "stop_typing", raw)
        self._typing_delegate[key] = pid
        await self._native(pid, "send_typing", raw, metadata)

    async def stop_typing(self, chat_id):  # pragma: no cover - needs live env
        _, raw = decode_chat_id(str(chat_id))
        pid = self._typing_delegate.pop(str(raw), None)
        if pid and pid in self._orig:
            await self._native(pid, "stop_typing", raw)


# -- standalone (out-of-process) cron sender -------------------------------
async def standalone_send(pconfig, chat_id, message, *, thread_id=None,
                          media_files=None, force_document=False):  # pragma: no cover
    """Cron delivery when the job runs outside the gateway process.

    Opens an ephemeral client for the resolved persona's token, sends, closes.
    """
    if not _DISCORD_AVAILABLE:
        raise RuntimeError("discord.py is not installed")

    from .routing import resolve_outbound_persona

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
