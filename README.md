# Hermes Persona Multiplexer 🎭

**One Hermes brain, many Discord faces.**

A [Hermes Agent](https://hermes-agent.nousresearch.com) `kind: platform` plugin
that connects **N Discord bot accounts through a single gateway process into one
shared‑brain agent**, choosing the persona by *which bot account received the
message*. DMs and channel messages reply as the right identity — **no webhooks,
no separate processes, one shared memory.**

> Status: **alpha (`0.1.0`, unreleased).** The decision core is implemented and
> tested; the live multi‑client adapter is validated via the two‑account spike
> (see [`docs/spike.md`](docs/spike.md)).

## Why this exists

Hermes is excellent but, today, **one gateway = one Discord token = one agent**.
If you want several distinct Discord bot identities (each with its own avatar)
backed by a *single* brain and memory, there's a gap:

| Approach | Distinct avatars | Brain / memory | Custom code |
|---|---|---|---|
| Hermes native multi‑agent ([#7517](https://github.com/NousResearch/hermes-agent/pull/25660)) | via routing | **N isolated** agents | — (unmerged) |
| Per‑user profile routing ([#33548](https://github.com/NousResearch/hermes-agent/issues/33548)) | ❌ one token | N profiles | — |
| Multi‑profile gateways | ✅ real bots | **N** brains (Honcho‑shared at best) | — |
| **This plugin** | ✅ real bots | **1 shared brain** ✅ | this plugin |

It's essentially **OpenClaw‑style multi‑account Discord, pointed at one Hermes
brain** — the capability people keep asking for after migrating from OpenClaw.

## How it works

```
@Alex ─┐
@Sam  ─┤  (N discord.py clients, one per token, in ONE process)
@Max  ─┼──► tag "persona" by receiving account ──► ONE gateway ──► ONE AIAgent
       ┘        ▲                                  (shared MEMORY/USER, skills)
                └── outbound replies routed back through the SAME persona's client
```

- **Inbound:** each client is labeled with its persona; messages are stamped with
  `persona=<id>` and forwarded to the single agent (shared `HERMES_HOME` ⇒ shared
  memory). DM @Alex → the brain answers as Alex; DM @Sam → as Sam.
- **Outbound:** replies route back through the originating persona's client, so
  the right avatar speaks — in DMs *and* shared channels.
- **No loops:** messages authored by any of our own persona accounts are ignored
  (the one brain decides who speaks). Pair with `DISCORD_ALLOW_BOTS=none`.

## Install

```bash
hermes plugins install https://github.com/gmoben/hermes-persona-multiplexer@v0.1.0
```

Enable it and configure personas in `~/.hermes/config.yaml` (see
[`examples/config.yaml`](examples/config.yaml)):

```yaml
plugins:
  enabled: [hermes-persona-multiplexer]

gateway:
  platforms:
    discord_personas:
      enabled: true
      extra:
        brain: main
        default_persona: alex
        personas:
          - { id: alex, token_env: DISCORD_TOKEN_ALEX }
          - { id: sam,  token_env: DISCORD_TOKEN_SAM }
```

Put the tokens in `~/.hermes/.env` and set
`DISCORD_PERSONAS_ALLOWED_USERS=<your-id>`. Then `hermes gateway restart`.

## Compatibility

| Plugin | Hermes (tested) | Notes |
|---|---|---|
| `0.1.0` | _TBD at first release_ | Tracks the dev‑channel `BasePlatformAdapter`. |

## Roadmap

- [ ] Two‑account spike: live multi‑client + send‑routing + loop‑prevention.
- [ ] Reuse the bundled Hermes Discord adapter per token (vs. raw `discord.py`).
- [ ] `standalone_sender_fn` for out‑of‑process cron delivery per persona.
- [ ] Optional per‑persona model/toolset overrides.
- [ ] Beyond Discord (the routing core is platform‑agnostic).

## License

[MIT](LICENSE) © gmoben
