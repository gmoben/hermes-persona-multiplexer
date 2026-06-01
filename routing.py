"""Core routing logic for the Hermes Persona Multiplexer.

This module is intentionally free of any Hermes or ``discord.py`` imports so the
decision logic — config parsing, persona resolution, loop prevention, and
outbound send routing — can be unit-tested without a running gateway or any
network access. All Hermes/discord wiring lives in :mod:`adapter`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

#: Platform name registered with the Hermes gateway.
PLATFORM_NAME = "discord_personas"

#: Leading tags the brain emits on a shared-channel message:
#:   ``[persona:<id>]`` — which persona's bot account delivers this reply.
#:   ``[next:<id>]``    — typing hint: who will deliver the upcoming reply (lets the
#:                        orchestrator's quick ack switch the typing indicator to the
#:                        answering persona without changing who sends the ack).
#: Both are parsed + stripped by the adapter's send path (see
#: :func:`extract_reply_persona` / :func:`extract_next_persona`).


class ConfigError(ValueError):
    """Raised when the multiplexer configuration is invalid."""


@dataclass(frozen=True)
class PersonaConfig:
    """A single persona = one real Discord bot account."""

    id: str
    token_env: str
    application_id: str | None = None
    display_name: str | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.id


@dataclass(frozen=True)
class MultiplexerConfig:
    """Resolved configuration for the multiplexer.

    ``brain`` is the Hermes agent/profile every persona routes to — this is the
    single shared brain. ``default_persona`` is used for proactive/cron sends
    that don't originate from an inbound message; it falls back to the first
    configured persona. ``orchestrator`` is the persona that intakes shared
    home-channel messages (so a channel message reaches the brain once, not once
    per persona); the brain then replies as the right specialist. It defaults to
    ``default_persona``.
    """

    brain: str
    personas: tuple[PersonaConfig, ...]
    default_persona: str
    orchestrator: str
    ignore_self: bool = True

    def has(self, persona_id: str) -> bool:
        return any(p.id == persona_id for p in self.personas)

    def persona(self, persona_id: str) -> PersonaConfig:
        for p in self.personas:
            if p.id == persona_id:
                return p
        raise KeyError(persona_id)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(p.id for p in self.personas)


@dataclass(frozen=True)
class ProcessDecision:
    """Outcome of deciding whether to process an inbound message."""

    process: bool
    reason: str
    persona: str | None = None


def parse_config(raw: dict | None) -> MultiplexerConfig:
    """Validate and normalize the ``discord_personas`` config block.

    Expected shape::

        discord_personas:
          brain: main
          ignore_self: true            # optional, default true
          default_persona: alex        # optional, defaults to first persona
          orchestrator: alex           # optional, intakes shared-channel msgs;
                                       #   defaults to default_persona
          personas:
            - id: alex
              token_env: DISCORD_TOKEN_ALEX
              application_id: "123"     # optional
              display_name: Alex        # optional
    """
    raw = raw or {}
    brain = str(raw.get("brain", "main")).strip() or "main"

    raw_personas = raw.get("personas") or []
    if not isinstance(raw_personas, list) or not raw_personas:
        raise ConfigError("`personas` must be a non-empty list")

    personas: list[PersonaConfig] = []
    for i, entry in enumerate(raw_personas):
        if not isinstance(entry, dict):
            raise ConfigError(f"persona #{i} must be a mapping, got {type(entry).__name__}")
        pid = str(entry.get("id", "")).strip()
        token_env = str(entry.get("token_env", "")).strip()
        if not pid:
            raise ConfigError(f"persona #{i} is missing `id`")
        if not token_env:
            raise ConfigError(f"persona '{pid}' is missing `token_env`")
        app_id = entry.get("application_id")
        personas.append(
            PersonaConfig(
                id=pid,
                token_env=token_env,
                application_id=str(app_id) if app_id is not None else None,
                display_name=(str(entry["display_name"]) if entry.get("display_name") else None),
            )
        )

    ids = [p.id for p in personas]
    dupe_ids = sorted({i for i in ids if ids.count(i) > 1})
    if dupe_ids:
        raise ConfigError(f"duplicate persona id(s): {dupe_ids}")

    envs = [p.token_env for p in personas]
    dupe_envs = sorted({e for e in envs if envs.count(e) > 1})
    if dupe_envs:
        raise ConfigError(f"duplicate token_env across personas: {dupe_envs}")

    default_persona = raw.get("default_persona")
    if default_persona is not None:
        default_persona = str(default_persona).strip()
        if default_persona not in ids:
            raise ConfigError(f"default_persona '{default_persona}' is not a configured persona")
    else:
        default_persona = personas[0].id

    orchestrator = raw.get("orchestrator")
    if orchestrator is not None:
        orchestrator = str(orchestrator).strip()
        if orchestrator not in ids:
            raise ConfigError(f"orchestrator '{orchestrator}' is not a configured persona")
    else:
        orchestrator = default_persona

    return MultiplexerConfig(
        brain=brain,
        personas=tuple(personas),
        default_persona=default_persona,
        orchestrator=orchestrator,
        ignore_self=bool(raw.get("ignore_self", True)),
    )


def is_self_authored(author_account_id: str | None, own_account_ids: Iterable[str]) -> bool:
    """True if a message was authored by one of our own persona bot accounts.

    This is the primary cascade/loop guard: the single brain controls every
    persona, so it must never react to messages its own personas posted.
    """
    if author_account_id is None:
        return False
    return str(author_account_id) in {str(a) for a in own_account_ids if a is not None}


def decide_inbound(
    *,
    recipient_persona: str,
    author_account_id: str | None,
    own_account_ids: Iterable[str],
    config: MultiplexerConfig,
    is_dm: bool = True,
    channel_id: str | None = None,
    home_channel_id: str | None = None,
) -> ProcessDecision:
    """Decide whether to process an inbound message and which persona owns it.

    ``recipient_persona`` is the persona whose bot account received the message
    (each Discord client is labeled with its persona at construction time).

    DMs are handled by the addressed persona (one bot = one persona). A **shared
    channel** message is seen by every persona's client, so to reach the brain
    exactly once it is only intaken by the ``orchestrator`` persona, and only in
    the configured ``home_channel_id`` (channels are ignored entirely when no home
    channel is configured). The brain then classifies the content and replies as
    the right specialist via a reply-routing tag (see :func:`extract_reply_persona`).
    """
    if not config.has(recipient_persona):
        return ProcessDecision(False, f"unknown-persona:{recipient_persona}", None)
    if config.ignore_self and is_self_authored(author_account_id, own_account_ids):
        return ProcessDecision(False, "own-account", recipient_persona)
    if is_dm:
        return ProcessDecision(True, "ok", recipient_persona)
    # Shared-channel message.
    if home_channel_id is None or str(channel_id) != str(home_channel_id):
        return ProcessDecision(False, "outside-home-channel", None)
    if recipient_persona != config.orchestrator:
        return ProcessDecision(False, "not-orchestrator", None)
    return ProcessDecision(True, "ok-channel", recipient_persona)


def should_intake_shared_channel(
    recipient_persona: str,
    channel_id: str | None,
    parent_channel_id: str | None,
    *,
    config: MultiplexerConfig,
    home_channel_id: str | None,
) -> bool:
    """Pre-gate for a non-DM message, applied *before* the delegate builds the event.

    A shared channel is seen by every persona's bot, and the platform may auto-create
    a thread per message — so without this gate, N delegates would each spawn a thread
    and try to intake the same message. Only the ``orchestrator`` intakes the shared
    channel **and its threads**, confined to the home channel (matched against the
    message's own channel or, for a thread, its ``parent_channel_id``). DMs bypass this
    entirely (each bot owns its own DM). Mirrors the channel half of
    :func:`decide_inbound`, but runs early so only one delegate proceeds.
    """
    if recipient_persona != config.orchestrator:
        return False
    if not home_channel_id:
        return False
    home = str(home_channel_id)
    return home in (str(channel_id or ""), str(parent_channel_id or ""))


def _extract_leading_persona_tag(
    content: str, keyword: str, valid_ids: Iterable[str]
) -> tuple[str | None, str]:
    """Pull a leading ``[<keyword>:<id>]`` tag naming a known persona.

    Returns ``(persona_id, content_without_tag)`` on a match, else ``(None, content)``
    unchanged (an unknown persona is left untouched).
    """
    if not content:
        return None, content
    m = re.match(rf"^\s*\[{keyword}:\s*([A-Za-z0-9_-]+)\s*\]\s*", content)
    if not m:
        return None, content
    canonical = {str(v).lower(): str(v) for v in valid_ids}
    resolved = canonical.get(m.group(1).strip().lower())
    if resolved is None:
        return None, content
    return resolved, content[m.end():]


def extract_reply_persona(content: str, valid_ids: Iterable[str]) -> tuple[str | None, str]:
    """``[persona:<id>]`` — which persona's bot account delivers this reply."""
    return _extract_leading_persona_tag(content, "persona", valid_ids)


def extract_next_persona(content: str, valid_ids: Iterable[str]) -> tuple[str | None, str]:
    """``[next:<id>]`` — typing hint naming who will deliver the upcoming reply.

    The orchestrator's quick ack uses this so the typing indicator switches to the
    answering persona while the ack text itself is still delivered by the orchestrator.
    """
    return _extract_leading_persona_tag(content, "next", valid_ids)


def split_reply_persona(
    content: str, valid_ids: Iterable[str]
) -> tuple[str | None, str, str]:
    """Locate a ``[persona:<id>]`` tag anywhere in the reply and split around it.

    Returns ``(persona_id, before, after)`` where ``before`` is the text preceding
    the tag (e.g. an orchestrator ack the brain bundled into the same message) and
    ``after`` is the persona's actual reply (tag removed). Returns ``(None, content, "")``
    when no tag naming a known persona is present. Unlike :func:`extract_reply_persona`
    (leading-only), this catches the tag even mid-message so it never leaks to the user.
    """
    if not content:
        return None, content, ""
    canonical = {str(v).lower(): str(v) for v in valid_ids}
    for m in re.finditer(r"\[persona:\s*([A-Za-z0-9_-]+)\s*\]", content):
        resolved = canonical.get(m.group(1).strip().lower())
        if resolved is not None:
            return resolved, content[: m.start()], content[m.end():].lstrip("\n ")
    return None, content, ""


def resolve_outbound_persona(
    *,
    explicit: str | None,
    inbound: str | None,
    config: MultiplexerConfig,
) -> str:
    """Pick which persona's client an outbound message goes through.

    Priority: explicit (e.g. a cron job or the agent naming a persona) →
    the persona of the inbound message being replied to → the configured
    default persona.
    """
    if explicit is not None:
        explicit = str(explicit).strip()
        if not config.has(explicit):
            raise KeyError(f"unknown outbound persona '{explicit}'")
        return explicit
    if inbound is not None and config.has(inbound):
        return inbound
    return config.default_persona


def persona_metadata(persona_id: str) -> dict:
    """Metadata stamped onto a normalized inbound event so the brain (and the
    send path) know which persona this conversation belongs to."""
    return {"persona": persona_id}
