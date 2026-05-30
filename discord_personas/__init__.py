"""Hermes Persona Multiplexer — one brain, many Discord faces.

A Hermes Agent ``kind: platform`` plugin that connects N Discord bot accounts
through a single gateway process into one shared-brain agent, selecting the
persona by which bot account received the message.

The package is split so the decision logic (:mod:`discord_personas.routing`,
:mod:`discord_personas.locks`) has no Hermes/discord dependency and is unit
testable, while :mod:`discord_personas.adapter` holds the gateway wiring.
"""

from __future__ import annotations

# Source of truth for the package version is ``plugin.yaml``; release-please
# keeps this literal in sync via its extra-files updater (annotation below).
__version__ = "0.1.0"  # x-release-please-version

from .routing import (  # noqa: E402
    PLATFORM_NAME,
    ConfigError,
    MultiplexerConfig,
    PersonaConfig,
    ProcessDecision,
    decide_inbound,
    parse_config,
    persona_metadata,
    resolve_outbound_persona,
)

__all__ = [
    "__version__",
    "PLATFORM_NAME",
    "ConfigError",
    "MultiplexerConfig",
    "PersonaConfig",
    "ProcessDecision",
    "decide_inbound",
    "parse_config",
    "persona_metadata",
    "resolve_outbound_persona",
    "register",
]


def register(ctx):
    """Hermes plugin entry point. Imported lazily so this package can be
    imported (for testing the core) without Hermes installed."""
    from .adapter import register as _register

    return _register(ctx)
