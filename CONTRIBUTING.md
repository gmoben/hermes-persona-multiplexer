# Contributing

Thanks for your interest in the Hermes Persona Multiplexer.

## Dev setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest          # pure core, no Hermes/discord required
ruff check .
```

## Architecture rule (important)

Keep the decision logic free of Hermes/`discord.py` imports:

- `routing.py` is **pure** — no `gateway.*`, no `discord` imports. This is what
  makes the core unit-testable in CI without a Hermes install.
- `adapter.py` holds all Hermes/discord wiring — it composes the bundled
  `DiscordAdapter` per persona. Its Hermes/discord imports are guarded so the
  module still imports for `register()` + pure-core tests.

If you add behavior, put the *decision* in `routing.py` (with tests) and the
*I/O* in `adapter.py`.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/). Version bumps and
the changelog are derived automatically (see [RELEASING.md](RELEASING.md)).

Scopes used here: `adapter`, `routing`, `config`, `docs`, `ci`, `release`.

```
feat(routing): add per-persona model override
fix(adapter): route cron sends through the resolved persona client
docs: document the composition architecture
```

`feat:` → minor, `fix:` → patch, `feat!:` / `BREAKING CHANGE:` → major (see the
pre-1.0 rule in RELEASING.md).

## Before opening a PR

- `pytest` and `ruff check .` pass.
- New decision logic has tests.
- If behavior or config changed, note it in the PR; the changelog is generated
  from your commit messages.
