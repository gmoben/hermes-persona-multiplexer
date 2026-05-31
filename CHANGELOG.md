# Changelog

## [0.4.3](https://github.com/gmoben/hermes-persona-multiplexer/compare/v0.4.2...v0.4.3) (2026-05-31)


### Bug Fixes

* deliver LLM file attachments natively instead of as a dead path ([6fe652d](https://github.com/gmoben/hermes-persona-multiplexer/commit/6fe652d6e6c7ecabcae18c3fa506be9bacfb333f))

## [0.4.2](https://github.com/gmoben/hermes-persona-multiplexer/compare/v0.4.1...v0.4.2) (2026-05-31)


### Bug Fixes

* forward image attachments and split mid-message [persona:] tags ([c9ff7b3](https://github.com/gmoben/hermes-persona-multiplexer/commit/c9ff7b347ffa76e7c6a50b622701887e0ea87d20))

## [0.4.1](https://github.com/gmoben/hermes-persona-multiplexer/compare/v0.4.0...v0.4.1) (2026-05-31)


### Bug Fixes

* typing indicator follows the answering persona, not the orchestrator ([42fb032](https://github.com/gmoben/hermes-persona-multiplexer/commit/42fb032f367a337a80623048c6b7bb29b7478e05))

## [0.4.0](https://github.com/gmoben/hermes-persona-multiplexer/compare/v0.3.0...v0.4.0) (2026-05-31)


### Features

* orchestrator sends a quick ack before the slow work (shared channel) ([71d9d0c](https://github.com/gmoben/hermes-persona-multiplexer/commit/71d9d0c7cca6d27fbe1faed5aa78ef568b0e0bb7))

## [0.3.0](https://github.com/gmoben/hermes-persona-multiplexer/compare/v0.2.0...v0.3.0) (2026-05-31)


### Features

* content-led shared-channel routing (orchestrator intake + reply tags) ([b8b71fc](https://github.com/gmoben/hermes-persona-multiplexer/commit/b8b71fcb9b7497f51cc1defa11c33237d147e8c9))

## [0.2.0](https://github.com/gmoben/hermes-persona-multiplexer/compare/v0.1.0...v0.2.0) (2026-05-31)


### ⚠ BREAKING CHANGES

* plugin renamed discord-personas -> hermes-persona-multiplexer; update plugins.enabled accordingly. Plugin modules moved from the discord_personas/ subpackage to the repository root.

### Features

* flatten to canonical layout and rename to hermes-persona-multiplexer ([a512fe5](https://github.com/gmoben/hermes-persona-multiplexer/commit/a512fe5c883a9d669795bf46932f135099d07ee3))

## 0.1.0 (2026-05-31)


### Features

* initial scaffold — persona multiplexer plugin ([3ead799](https://github.com/gmoben/hermes-persona-multiplexer/commit/3ead7999ae87128ed8e38ae8e7cd83ba3d9ccd7d))


### Miscellaneous

* prepare first release as 0.1.0 ([8a6b59e](https://github.com/gmoben/hermes-persona-multiplexer/commit/8a6b59e510e48422efa44e57c57cb211964dfe86))

## Changelog

All notable changes to this project are documented here. This file is maintained
**automatically by [release-please](https://github.com/googleapis/release-please)**
from [Conventional Commits](https://www.conventionalcommits.org/) — do not edit it
by hand. The format follows [Keep a Changelog](https://keepachangelog.com/) and the
project adheres to [Semantic Versioning](https://semver.org/). See
[RELEASING.md](RELEASING.md).
