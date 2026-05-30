# AGENTS.md — Coding Agent Guidelines

Guidelines for AI coding agents (and humans) working in the
**hermes-persona-multiplexer** repository.

## Secrets and Sensitive Data

**NEVER print, echo, cat, log, or commit secrets, tokens, or credentials.** For
this project that specifically means:

- Discord **bot tokens** (one per persona, e.g. `DISCORD_TOKEN_ALEX`)
- `DISCORD_PERSONAS_ALLOWED_USERS` and any channel/user IDs you treat as private
- Anything in `.env` (only `.env.example` — names, never values — is committed)
- Any variable whose name contains `TOKEN`, `SECRET`, `KEY`, `PASSWORD`, or `AUTH`

To verify a secret is present without revealing it:

```bash
# GOOD — presence + length only
python3 -c "import os; t=os.getenv('DISCORD_TOKEN_ALEX',''); print('set:', bool(t), 'len:', len(t))"

# BAD — never do this
echo $DISCORD_TOKEN_ALEX
grep TOKEN ~/.hermes/.env
```

## Maintaining This Document

When you change the repo, **update AGENTS.md in the same commit** if you:

- add/rename modules, config keys, or env vars
- change how routing, locking, or registration works
- add a new release/CI step or change the versioning workflow

## Project Overview

A Hermes Agent `kind: platform` plugin: connects N Discord bot accounts through
one gateway into a single shared‑brain agent; persona is chosen by the receiving
account. See [README.md](README.md) for the user‑facing description.

**Primary language:** Python (3.11+). Config: YAML. CI: GitHub Actions.

## Repository Structure

```
├── discord_personas/
│   ├── routing.py      # PURE decision core — no Hermes/discord imports (tested)
│   ├── locks.py        # PURE scoped-lock manager + graceful degradation (tested)
│   ├── adapter.py      # Hermes/discord wiring; guarded imports; register() entry point
│   └── __init__.py     # version + lazy register()
├── tests/              # pytest; runs with zero Hermes/discord deps
├── examples/config.yaml
├── plugin.yaml         # kind: platform manifest (version = source of truth)
├── docs/spike.md       # the two-account validation plan
├── cliff.toml          # git-cliff (sole CHANGELOG writer)
├── release-please-config.json + .release-please-manifest.json
└── .github/workflows/  # ci.yml (lint+test), release.yml (release-please + git-cliff)
```

## Build / Test / Verify

```bash
pip install -e ".[dev]"
pytest                 # core suite — no Hermes/discord required
ruff check .
```

**Always validate before committing:** `pytest` and `ruff check .` must pass.
The live adapter (`connect`/`send`) needs a real Hermes + Discord environment and
is verified through the spike in `docs/spike.md`, not in CI.

## Architecture Rule

Keep decisions pure and I/O separate:

- `routing.py` / `locks.py` — **no** `gateway.*` or `discord` imports. This is
  what lets CI test the logic with no Hermes install.
- `adapter.py` — all Hermes/discord wiring; Hermes imports are guarded so
  `register()` stays importable/testable.

New behavior → put the *decision* in `routing.py` (with a test), the *I/O* in
`adapter.py`.

## Versioning & Releases

SemVer; **release-please** owns version/tag/release + version sync, **git-cliff**
is the sole `CHANGELOG.md` writer. Full rules and the Hermes‑compatibility
recording requirement are in [RELEASING.md](RELEASING.md).

## Commit Messages

[Conventional Commits](https://www.conventionalcommits.org/). Scopes: `adapter`,
`routing`, `locks`, `config`, `docs`, `ci`, `release`. `feat:`→minor, `fix:`→patch,
`feat!:`/`BREAKING CHANGE:`→major (pre‑1.0: minor=breaking, patch=fix).

## Code Style

- `#!/usr/bin/env python3`; format/lint with `ruff` (line length 100).
- `snake_case` functions, `PascalCase` classes, `UPPER_CASE` constants.
- Prefer stdlib; the routing/lock core must stay dependency‑free.
- Adapter handlers must degrade, never crash the gateway (one bad token disables
  only that persona).
