"""Hermes Persona Multiplexer — one brain, many Discord faces.

A Hermes Agent ``kind: platform`` plugin that connects N Discord bot accounts
through a single gateway process into one shared-brain agent, selecting the
persona by which bot account received the message.

The package is split so the decision logic (``routing``, ``locks``) has no
Hermes/discord dependency and is unit-testable, while ``adapter`` holds the live
gateway wiring. This is the canonical flat plugin layout: Hermes loads the plugin
*directory* as a package, so the modules import each other relatively. The
``except ImportError`` fallback below only fires when this file is imported
standalone (e.g. pytest collecting it directly), where there's no parent package.
"""

from __future__ import annotations

# Source of truth for the package version is ``plugin.yaml``; release-please
# keeps this literal in sync via its extra-files updater (annotation below).
__version__ = "0.2.0"  # x-release-please-version

try:
    from .routing import (
        PLATFORM_NAME,
        ConfigError,
        MultiplexerConfig,
        PersonaConfig,
        ProcessDecision,
        decide_inbound,
        extract_reply_persona,
        parse_config,
        persona_metadata,
        resolve_outbound_persona,
    )
except ImportError:  # imported standalone, no parent package
    from routing import (  # type: ignore[no-redef]
        PLATFORM_NAME,
        ConfigError,
        MultiplexerConfig,
        PersonaConfig,
        ProcessDecision,
        decide_inbound,
        extract_reply_persona,
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
    "extract_reply_persona",
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
