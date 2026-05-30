# Releasing

This project uses **two** tools with strictly separated responsibilities so they
never fight over the changelog:

| Tool | Owns |
|---|---|
| **release-please** | Version decision (SemVer from Conventional Commits), the release PR, the git **tag**, the GitHub Release, and **syncing the version** into `plugin.yaml`, `pyproject.toml`, and `discord_personas/__init__.py`. |
| **git-cliff** | The **sole writer** of `CHANGELOG.md` and the GitHub Release-notes body. |

To avoid both writing the changelog, release-please is configured with
`"changelog-path"` pointing at a file it does **not** manage, and the
`extra-files` updater handles the version strings; git-cliff regenerates
`CHANGELOG.md` in CI on tag.

## Versioning rules (SemVer)

| Bump | When |
|---|---|
| **MAJOR** | Breaking config schema or behavior; dropping a supported Hermes line. e.g. restructuring the `personas` map, changing the inbound `persona` tagging contract. |
| **MINOR** | Backward-compatible features / new opt-in config. e.g. per-persona model override, a new routing option with a safe default. |
| **PATCH** | Fixes, docs, and **dev-channel compatibility shims** that don't change this plugin's own API (e.g. adapting to a `BasePlatformAdapter` change upstream). |

**Pre-1.0 rule (we are `0.x`):** MINOR = breaking-or-notable, PATCH = fix/compat.
Because consumers pin an exact tag, breakage is always opt-in.

**`1.0.0` criteria:** the persona-map schema is frozen, the multiplexer has run
stably across at least one full Hermes release, and the adapter-reuse risk from
the spike is retired.

## Hermes compatibility (record every release)

This plugin tracks the dev-channel `BasePlatformAdapter` interface. Every release
**must** record the tested Hermes version/commit in the release notes, and the
README compatibility matrix is updated. `plugin.yaml` may carry a
`hermes_min_version` once the floor is known.

## Cadence

- `v0.1.0` is cut after the two-account spike validates (see `docs/spike.md`).
- Batch fixes into a PATCH — don't tag every commit.
- Security fixes ship immediately as a PATCH with a `### Security` changelog note.
- Use `-rc.N` pre-release tags for risky adapter changes so downstream consumers
  can test before pinning an exact tag.

## Manual fallback (if CI is unavailable)

```bash
# 1) bump version in plugin.yaml, pyproject.toml, discord_personas/__init__.py
# 2) regenerate changelog
git cliff --tag vX.Y.Z -o CHANGELOG.md
# 3) commit, tag, push
git commit -am "chore(release): vX.Y.Z"
git tag -a vX.Y.Z -m "vX.Y.Z"
git push --follow-tags
```
