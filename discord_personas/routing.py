"""Core routing logic for the Hermes Persona Multiplexer.

This module is intentionally free of any Hermes or ``discord.py`` imports so the
decision logic — config parsing, persona resolution, loop prevention, and
outbound send routing — can be unit-tested without a running gateway or any
network access. All Hermes/discord wiring lives in :mod:`discord_personas.adapter`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

#: Platform name registered with the Hermes gateway.
PLATFORM_NAME = "discord_personas"


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
    configured persona.
    """

    brain: str
    personas: tuple[PersonaConfig, ...]
    default_persona: str
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

    return MultiplexerConfig(
        brain=brain,
        personas=tuple(personas),
        default_persona=default_persona,
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
) -> ProcessDecision:
    """Decide whether to process an inbound message and which persona owns it.

    ``recipient_persona`` is the persona whose bot account received the message
    (each Discord client is labeled with its persona at construction time).
    """
    if not config.has(recipient_persona):
        return ProcessDecision(False, f"unknown-persona:{recipient_persona}", None)
    if config.ignore_self and is_self_authored(author_account_id, own_account_ids):
        return ProcessDecision(False, "own-account", recipient_persona)
    return ProcessDecision(True, "ok", recipient_persona)


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
