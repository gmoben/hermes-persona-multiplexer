# Architecture

This plugin runs **N Discord bot identities through one Hermes gateway into a single
shared‑brain agent**. It does so by **composing the bundled Hermes Discord adapter**
rather than re‑implementing Discord — each persona is a real `DiscordAdapter`
instance, and the plugin is a thin router over them.

> Historical note: this started as a two‑account spike to prove multiple bundled
> Discord adapters could run in one process and feed one brain. That risk is
> resolved — composition is the realized design described here.

## Components

- **`routing.py` — the pure decision core.** No `gateway.*` / `discord` imports, so
  it's unit‑tested with zero dependencies. It owns: config parsing
  (`parse_config`), persona resolution, the `[persona:]` / `[next:]` tag parsing,
  shared‑channel intake (`decide_inbound`, `should_intake_shared_channel`), the user
  allowlist (`is_allowed_user`), and outbound text tidy (`tidy_outbound_text`).
- **`adapter.py` — the router + live wiring.** Registers one platform
  (`discord_personas`) and, on connect, builds one bundled `DiscordAdapter`
  *delegate* per persona, wiring inbound and intercepting outbound.

```
@Alex ─┐  delegate = bundled DiscordAdapter (own token + discord.py client)
@Sam  ─┤
@Max  ─┼──► router re‑tags persona ──► gateway _message_handler ──► one agent
       ┘          ▲                                                  (shared memory)
                  └── reply routed to the answering persona's delegate
```

## The key constraint: adapters own their send

A Hermes adapter **sends its own reply**. `delegate.handle_message(event)` builds the
session key, calls the message handler to get the agent's response, and then delivers
it via `delegate.send(event.source.chat_id)`. There is no "gateway routes the reply by
platform" step for the primary reply.

That shapes everything:

- **We do not encode the persona into `chat_id`.** An early design stamped
  `p!<persona>!<chat>` into `chat_id`; the delegate's own send then choked on
  `int(chat_id)`, and (because the session key is built *before* our handler runs) the
  encoding namespaced nothing. The chat id is left raw.
- **We route at `delegate.send`.** Each delegate's `send` is wrapped so the brain's
  `[next:]` / `[persona:]` tags are split out and the reply is delivered through the
  *answering* persona's native send (`_route_send` → the captured native methods in
  `_orig`).
- **Platform aliasing.** Each delegate's `platform` is aliased to the router's
  (`discord_personas`) so *gateway‑initiated* outbound (media, typing, cron) is
  dispatched to the router, which forwards it to the right persona via the
  `_chat_persona` / `_reply_persona` maps.

## Inbound

1. The delegate's `on_message` is replaced with a minimal gate (dedup, ignore
   self/bots/system) plus a pre‑gate for shared channels:
   `should_intake_shared_channel` lets **only the orchestrator** run `_handle_message`
   for a channel/thread message — so N delegates don't each spawn a thread.
2. The delegate's own `_handle_message` still runs (we reuse it), so **native
   attachment caching** (image/audio/voice/document) and dedup apply.
3. The router's handler (`_dispatch_inbound`) gates by allowlist + `decide_inbound`,
   stamps a per‑message `channel_prompt` (persona voice in DMs / orchestrator routing
   prompt in the shared channel), and forwards to the gateway. The chat id is left
   raw; for an auto‑created thread the home‑channel check uses the thread's parent.

Intake diagnostics distinguish `gateway_message_create`, `received`, delegate
dispatch/rejection, and `brain_dispatch`/`brain_returned`. They record only the
persona, stage, routing/access booleans and attachment counts: never message text,
payloads, filenames, URLs or private IDs. The native socket-event callback still
runs so its liveness timestamp is preserved. Confirm a real incoming upload in a
throwaway channel/thread reaches the transcript and produces one final reply/record;
an online status and an outbound/manual recovery reply do not prove intake works.
Native `missed_message_backfill` (re-execution) is explicitly disabled on delegates:
it bypasses the router's orchestrator gate and must not compete with a user's
repost. This does not disable read-only `history_backfill` conversation context.

The gateway's builtin Discord YAML-to-env bridge exports `DISCORD_REQUIRE_MENTION`
and `DISCORD_AUTO_THREAD` defaults even when this plugin supplies the transport.
Modern delegates prefer those env values over `config.extra`, so setting only the
extra flags is insufficient. The router pins those two routing flags off through
the delegate's native flag resolver; all other native flags and access controls
are unchanged. Integration checks load gateway configuration before intake so
this startup-dependent failure cannot be hidden by a read-only-config test.

## Outbound

- **Text:** `delegate.send` (wrapped) → `_route_send` splits `[next:]`/`[persona:]`,
  delivers the orchestrator ack and the persona reply through the right delegates,
  and runs `tidy_outbound_text` to drop dangling `MEDIA:` labels.
- **Attachments:** the brain emits `MEDIA:/path`; the gateway extracts + validates it
  and calls the router's `send_document` / `send_image_file` / … which route to the
  answering persona's native uploader (a real `discord.File`).
- **Typing:** follows the answering persona (set from the turn's `[next:]`/`[persona:]`
  tag), switching delegates mid‑turn. Each delegate's `send_typing` is wrapped, so
  *every* typing request — including the base class's ~2s `_keep_typing` refreshes
  during processing — funnels through one per‑chat state machine: exactly one persona
  types at a time, and the orchestrator never keeps typing after handing off via
  `[next:]`.

## Access control & safety

- **Allowlist:** `DISCORD_PERSONAS_ALLOWED_USERS` (opt‑in). Enforced early in
  `on_message` (before any caching/agent work) and again in `_dispatch_inbound`.
- **Loop prevention:** bot‑authored messages are dropped, so personas never react to
  each other.
- **Degradation:** each delegate connects independently; a bad token disables only
  that persona. Streaming edits are disabled (`SUPPORTS_MESSAGE_EDITING = False`)
  because the answering persona isn't known until the `[persona:]` tag — we deliver
  one final tagged message.
- **Runtime recovery:** delegates forward fatal errors through the router's
  gateway callback. Native liveness watchdogs may close a failed Discord client;
  the runner must then rebuild the router and its delegates (a bad token is skipped
  on reconnect, not promoted to a permanent crew-wide failure). Concurrent failures
  trigger one reconnect, stale/teardown callbacks are ignored, and a delegate that
  dies during startup fails the connection before it can be marked healthy.

## What we inherit (vs. re‑implement)

Because each persona is a real `DiscordAdapter`: inbound + outbound attachments,
history backfill, message formatting/chunking, reply references, rate‑limit handling,
and typing — all native. This plugin only adds persona selection, shared‑channel
orchestration, and access control on top.
