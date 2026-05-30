# Two-Account Spike

The one real technical risk in this plugin is **running multiple Discord clients
in a single process and feeding them all into one Hermes brain** — specifically,
whether the bundled Hermes Discord adapter can be safely instantiated more than
once (shared global state?), and whether inbound persona tagging + outbound
send-routing + loop-prevention behave under a live gateway.

This spike validates exactly that, with **two** bot accounts, before generalizing
to N and cutting `v0.1.0`.

## Goal

Two Discord bots (`alex`, `sam`) → one Hermes agent (shared `HERMES_HOME`,
shared memory). Prove:

1. Both clients connect in one `hermes gateway` process (per-token scoped locks held).
2. A DM to **@alex** is answered **as alex**; a DM to **@sam** is answered **as sam**;
   both turns share the same memory/profile.
3. A reply routes back through the **originating** persona's client (correct avatar).
4. The brain never reacts to messages its own personas posted (no loops) in a shared channel.
5. Killing/invalidating one token degrades only that persona; the other stays online.

## Prerequisites

- A Hermes install (dev channel) with the bundled Discord adapter present
  (`plugins/platforms/discord/adapter.py`).
- Two Discord applications + bot tokens (Message Content + Server Members intents).
- This plugin enabled: `plugins.enabled: [discord-personas]`.
- `~/.hermes/.env`: `DISCORD_TOKEN_ALEX`, `DISCORD_TOKEN_SAM`,
  `DISCORD_PERSONAS_ALLOWED_USERS=<your id>`, `DISCORD_ALLOW_BOTS=none`.

## Implementation checklist (fills the `TODO(spike)` markers in `adapter.py`)

- [ ] `connect()`: per persona, build a Discord client from the token, wire
      `on_message` → `_on_inbound(recipient_persona=…, author_account_id=…, …)`,
      capture each bot's own user id into `self._own_account_ids`.
      Prefer reusing the bundled `DiscordAdapter` per token; fall back to a raw
      `discord.py` client if shared state prevents multiple instances.
- [ ] `send()`: complete the `client.send(raw_chat_id, …)` call for the resolved persona.
- [ ] `standalone_send()`: ephemeral client per persona token for out-of-process cron.

## Success criteria → `v0.1.0`

All five goals pass manually, plus the existing core suite stays green
(`pytest`). Record the **tested Hermes version/commit** in the release notes and
the README compatibility matrix (see `RELEASING.md`).

## If adapter reuse fails

If instantiating the bundled adapter N times proves unsafe (global singletons),
the fallback is a thin raw-`discord.py` client per persona that builds a
`MessageEvent` and calls `self.handle_message(...)` directly — the routing core
(`routing.py`) is unaffected either way, which is the whole point of keeping it
Hermes-independent.
