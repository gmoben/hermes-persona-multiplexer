# Releasing

Releases are fully automated by
[**release-please**](https://github.com/googleapis/release-please). It derives the
next version from Conventional Commits, maintains a release PR, and on merge it:

- creates the git **tag** and the **GitHub Release**,
- updates **`CHANGELOG.md`**,
- syncs the version into **`plugin.yaml`**, **`pyproject.toml`**, and
  **`__init__.py`** (via `extra-files`; the `__init__.py` line
  carries an `x-release-please-version` annotation for the generic updater).

You don't bump versions or edit the changelog by hand — just write good commit
messages and merge the release PR when you're ready to cut a release.

## Versioning rules (SemVer)

| Bump | When |
|---|---|
| **MAJOR** | Breaking config schema or behavior; dropping a supported Hermes line. e.g. restructuring the `personas` map, changing the inbound `persona` tagging contract. |
| **MINOR** | Backward-compatible features / new opt-in config. e.g. per-persona model override, a new routing option with a safe default. |
| **PATCH** | Fixes, docs, and **dev-channel compatibility shims** that don't change this plugin's own API (e.g. adapting to a `BasePlatformAdapter` change upstream). |

**Pre-1.0 rule (we are `0.x`):** the config is set so MINOR = breaking-or-notable
and PATCH = fix/compat (`bump-minor-pre-major: true`,
`bump-patch-for-minor-pre-major: false`). Because consumers pin an exact tag,
breakage is always opt-in.

**`1.0.0` criteria:** the persona-map schema is frozen and the composition (N
bundled `DiscordAdapter` delegates) has run stably across at least one full Hermes
release. Cut it by merging a commit with `Release-As: 1.0.0` in the body, or a
`feat!:` once you're ready for the major.

## Hermes compatibility (record every release)

This plugin tracks the dev-channel `BasePlatformAdapter` interface. When cutting a
release, record the tested Hermes version/commit in the GitHub Release notes and
update the README compatibility matrix. `plugin.yaml` may carry a
`hermes_min_version` once the floor is known.

## Cadence

- Releases are cut by merging the release-please PR — no manual tagging.
- Let changes accumulate — every merged `fix:`/`feat:` updates the pending release
  PR; merge it when you want the release.
- Security fixes: merge the fix and release promptly.

## Conventional Commits → changelog sections

`feat:` → Features, `fix:` → Bug Fixes, `perf:` → Performance, `refactor:` →
Refactors, `docs:` → Documentation. `test:`/`build:`/`ci:`/`chore:` are hidden
from the changelog (see `release-please-config.json`). `feat!:` or a
`BREAKING CHANGE:` footer triggers a major (or a minor pre-1.0).

## Manual fallback (only if Actions is unavailable)

```bash
# bump the three version locations + .release-please-manifest.json to X.Y.Z,
# add a CHANGELOG entry, then:
git commit -am "chore(release): vX.Y.Z"
git tag -a vX.Y.Z -m "vX.Y.Z"
git push --follow-tags
gh release create vX.Y.Z --generate-notes
```
