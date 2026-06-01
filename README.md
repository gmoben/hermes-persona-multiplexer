# Hermes Persona Multiplexer 🎭

**One Hermes brain, many Discord faces.**

A [Hermes Agent](https://hermes-agent.nousresearch.com) `kind: platform` plugin
that runs **N Discord bot accounts through a single gateway process into one
shared‑brain agent**, choosing the persona by *which bot account received the
message*. DMs and shared‑channel messages reply as the right identity — **no
webhooks, no separate processes, one shared memory.**

It works by **composing the bundled Hermes Discord adapter**: each persona is a
real `DiscordAdapter` instance (its own token + client), and this plugin is a thin
router over them. So every native Discord behavior — **inbound *and* outbound
attachments, history backfill, message chunking, rate‑limit handling, typing** —
is inherited rather than reimplemented.

## Why this exists

Hermes is excellent but, today, **one gateway = one Discord token = one agent**.
If you want several distinct Discord bot identities (each its own avatar) backed
by a *single* brain and memory, there's a gap:

| Approach | Distinct avatars | Brain / memory |
|---|---|---|
| Hermes native multi‑agent | via routing | **N isolated** agents |
| Per‑user profile routing | ❌ one token | N profiles |
| Multi‑profile gateways | ✅ real bots | **N** brains |
| **This plugin** | ✅ real bots | **1 shared brain** ✅ |

It's essentially **multi‑account Discord pointed at one Hermes brain** — the
capability people keep asking for after migrating from OpenClaw‑style setups.

## How it works

```
@Alex ─┐  each face = a bundled Hermes DiscordAdapter (its own token + client)
@Sam  ─┤
@Max  ─┼──► router re‑tags the persona ──► ONE gateway ──► ONE shared‑brain agent
       ┘          ▲                          (shared memory / profile / skills)
                  └─ reply routed to the answering persona's delegate (right avatar)
```

The plugin registers **one** platform (`discord_personas`) but runs **one bundled
`DiscordAdapter` delegate per persona**. On connect it wires each delegate's
inbound through the router and intercepts its outbound send; everything else is
the delegate's native behavior.

- **DMs:** each delegate owns its persona's DMs. A DM to **@Alex** is answered as
  **Alex**, **@Sam** as **Sam** — all sharing one `HERMES_HOME` (one memory).
- **Shared channel:** invite all the bots and set `DISCORD_PERSONAS_HOME_CHANNEL`.
  Every bot sees each message, so to reach the brain *once* only the
  **`orchestrator`** persona intakes it. The brain sends a quick one‑line ack as the
  orchestrator carrying a `[next:<id>]` typing hint, then starts its full reply with
  a `[persona:<id>]` tag — the router strips the tags and delivers the reply through
  that persona's bot. No home channel set ⇒ DM‑only.
- **Threads:** if Discord auto‑threading is on, only the orchestrator opens a thread
  and the routed persona answers **in that thread** (one thread per turn, not one per
  bot).
- **Attachments — both directions:**
  - *Inbound* — images, audio, voice notes, and documents (PDF/CSV/…) a user sends a
    persona are cached and handed to the brain (vision / file reading), inherited
    from the bundled adapter.
  - *Outbound* — when the brain emits a `MEDIA:/path` tag, the file is uploaded as a
    native Discord attachment by the answering persona.
- **Typing follows the answer:** the typing indicator switches from the orchestrator
  to whichever persona is about to reply.
- **Access control:** an opt‑in allowlist (`DISCORD_PERSONAS_ALLOWED_USERS`) gates
  who may interact — in DMs *and* the shared channel.
- **No loops:** messages authored by any of our own persona bots are ignored, and a
  bad token degrades only that persona (the rest stay online).

## Install

`hermes plugins install` clones a repo's default branch (no git‑ref pinning), so to
pin a release, tag‑clone then install from the local path:

```bash
git clone --branch v0.5.0 https://github.com/gmoben/hermes-persona-multiplexer.git /tmp/hpm
hermes plugins install file:///tmp/hpm && rm -rf /tmp/hpm
# or unpinned (tracks main): hermes plugins install gmoben/hermes-persona-multiplexer
hermes gateway restart
```

Requires a Hermes install that ships the bundled Discord adapter
(`plugins/platforms/discord/adapter.py`) — this plugin composes it.

## Configure

Enable the plugin and add the `discord_personas` platform to `~/.hermes/config.yaml`
(full example: [`examples/config.yaml`](examples/config.yaml)):

```yaml
plugins:
  enabled: [hermes-persona-multiplexer]

gateway:
  platforms:
    discord_personas:
      enabled: true
      extra:
        brain: main             # the single shared-brain agent every persona routes to
        default_persona: alex   # used for cron/proactive sends; defaults to the first persona
        orchestrator: alex      # intakes shared-channel messages (defaults to default_persona)
        personas:
          - { id: alex, token_env: DISCORD_TOKEN_ALEX, display_name: Alex }
          - { id: sam,  token_env: DISCORD_TOKEN_SAM,  display_name: Sam  }
          - { id: max,  token_env: DISCORD_TOKEN_MAX,  display_name: Max  }
```

Then in `~/.hermes/.env` (never committed) — the per‑persona tokens plus, optionally,
the shared channel and the user allowlist:

```bash
DISCORD_TOKEN_ALEX=...
DISCORD_TOKEN_SAM=...
DISCORD_TOKEN_MAX=...
DISCORD_PERSONAS_HOME_CHANNEL=123456789012345678   # shared channel id (omit for DM-only)
DISCORD_PERSONAS_ALLOWED_USERS=2841...96            # comma-separated; omit = open to everyone
```

Each persona needs the **Message Content** intent. `hermes gateway restart` to apply.

## Compatibility

| Plugin | Hermes (tested) | Notes |
|---|---|---|
| `0.5.x` | `0.15.x` | Composes the bundled `DiscordAdapter`; tracks the `BasePlatformAdapter` contract. |

## Design notes

The plugin keeps a **pure decision core** (`routing.py` — config parsing, persona
resolution, the `[persona:]`/`[next:]` tags, channel/allowlist gating) with **no
Hermes or `discord.py` imports**, so it's unit‑tested with zero dependencies. All
live wiring (delegate composition, send routing, typing) lives in `adapter.py`. See
[`docs/architecture.md`](docs/architecture.md) for the composition design and the
adapter‑owns‑send routing model.

## License

[MIT](LICENSE) © gmoben
