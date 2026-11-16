# connectors/whatsapp

Personal QR WhatsApp bridge: private Node 26 ESM package `@maraclaw/whatsapp-bridge` (Baileys `7.0.0-rc14`, Zod, pino) with its own lockfile, outside the Python project. The engine spawns one bridge per linked agent; the bridge holds no durable state. The Python parent owns credentials, authorization, quota, journals and dispatch. Parent code: `../../app/services/channels/whatsapp_personal_*.py`; operator behavior: `../../docs/channels.md`.

## Structure

`src/`: `bridge.ts` (main thread, stdio), `worker.ts` (worker entry), `runtime.ts` (lifecycle), `protocol.ts` (frames), `peer.ts` (framed output and RPC), `auth.ts` + `auth-codec.ts` (parent-backed credentials), `transport.ts` (Baileys), `input.ts` (self-chat intake), `sends.ts` (send outcomes), `diagnostics.ts`, `media_protocol.ts` + `media_offers.ts` + `media_transport.ts` (bounded inbound retrieval). `dist/` and `node_modules/` are gitignored; `dist/src/bridge.js` is the spawned entry.

## Protocol

- The supervisor runs `node dist/src/bridge.js` through `spawn_child`: stderr discarded, environment exactly `PATH`. Frames are newline-delimited JSON on stdin/stdout, each with `id` and a per-run `token`.
- Parent frames (`init`, `lease`, `shutdown`, `result`, `send`, `media_get`, `media_ack`, `media_error`) are strict Zod objects; child frames are `diagnostic`, `auth_get`, `auth_set`, `creds_set`, `ready`, `qr`, `identity`, `message`, `closed`, `send_result`, `media_chunk`, `media_end`, `media_error`. The Python mirror is `../../app/services/channels/whatsapp_personal_protocol.py` (`extra=forbid`): change both sides together.
- Frames are capped at 1 MiB. `Frames.push` enforces this on raw bytes before JSON parsing, even for an unterminated line, and decodes UTF-8 fatally. `Peer.emit` refuses larger output.
- stdout isolation: only the main thread writes stdout. Baileys runs in a `worker_threads` Worker whose stdout/stderr are discarded, so dependency output cannot corrupt framing. The worker returns finished lines; a non-string, unterminated, oversized or backlogged line ends the bridge. Never write to `console` or `process.stdout` elsewhere.
- The first frame must be `init` (15 s limit); its token is the capability. Later tokens are compared with `timingSafeEqual`. A second `init`, mismatch, malformed frame, stdin EOF, SIGTERM or SIGINT starts shutdown.
- Leases: `init` and `lease` carry `lease_ms` (parent sends 30000 about every second). The bridge exits if none arrives within `lease_ms` + 2.25 s; the runtime stops with `lease_expired`.
- Shutdown always ends with a `closed` frame whose reason is `logged_out`, `replaced`, `forbidden`, `transport_failed`, `auth_store_failed`, `lease_expired` or `shutdown`, then worker termination and exit.

## Credentials

- The bridge never reads or writes disk. `ParentAuth` implements the Baileys auth state through parent RPCs (`auth_get`, `auth_set`, `creds_set`) answered by `result` (15 s timeout, at most 128 pending). Mutations are serialized.
- Fail closed: a rejected, timed-out, malformed or undecodable exchange poisons the store and stops with `auth_store_failed`. `ParentAuth.barrier()` then rejects every dependent operation.
- Credentials are saved before the transport opens, and `transport.ts` wraps `socket.ws.send` so no frame reaches the wire before the barrier resolves (Baileys does not await `creds.update` listeners). Reconnects await the barrier too.
- `parseKey` has a parser for every `SignalDataTypeMap` category (compile-enforced); a new SDK category needs one. Keep `BufferJSON` round-trips lossless.

## Self-chat and sends

- `notifyText` forwards live `notify` text or original image/document/video offers from the account's own PN/LID chat: one content field, text/caption at most 4096 code points, no participant, quote, forwarding or stub, safe timestamp. Media additionally requires `fromMe: true`; groups and history stay excluded. Rejections use fixed `DropReason` values. Identity needs an authoritative PN; a LID alone is rejected.
- A `send` must target the exact normalized own JID and uses the parent-reserved `message_id` as the WhatsApp ID. Any other target is reported `rejected` and stops the bridge, so a replay can never become authorized later. Replays by `command_id` re-report the stored outcome; a conflicting replay, duplicate `message_id` or more than 10,000 commands also stops it.
- Outcomes: `accepted` only on a provider ack or status update; `rejected` only before anything reached the wire; `uncertain` after dispatch for any exception, timeout, disconnect or shutdown. Never retry an uncertain send or reuse its ID: Baileys retries, session recreation and message caches are disabled, and the parent also marks stale dispatches uncertain.
- Pairing: a QR is emitted only while unpaired, within 120 s. Close codes 401, 440 and 403 stop (`logged_out`, `replaced`, `forbidden`); other closes reconnect after 1, 2, 4, 8, 16 s (each scaled by 50-100% jitter; `RuntimeHost.jitter` is injectable), then `transport_failed`. Only a connection that stays open for 60 s resets that budget (the timer is armed on `open` and cleared on close, reopen and stop), so a flapping socket still ends; at most one reconnect timer is pending. A closing socket revokes intake and marks its pending sends uncertain before awaiting the barrier.
- Diagnostics use fixed enums and bounded numbers only: never QR, credentials, JIDs, token, message text or error text. A failing diagnostic write must not change authentication, forwarding or shutdown.

## Inbound media retrieval

- Offers contain metadata only, bound to the original message ID and source digest. Keep provider URLs, media keys and thumbnails inside the bridge; `media_get` retrieves an offered original after parent authorization. This flow retrieves inbound media; `send` remains text-only.
- `MediaTransport` allows one transfer and one outstanding chunk. Each chunk requires the exact transfer/sequence `media_ack` before the next; `media_end` carries final size and SHA-256. Parent `media_error` cancels the active transfer.
- Preserve the authentication barrier before download and current-socket checks before download/chunks. Revocation, close, invalid ACK, limit failure or deadline aborts the provider stream and releases transfer resources.
- Limits in `src/media_protocol.ts`: 64 KiB decoded chunks, 10 MiB images, 50 MiB other files, a fixed 120-second transfer deadline, and the smaller parent-requested byte cap. `test/media.test.ts` covers offers, exclusions, ACK backpressure and cancellation.

## Commands

```bash
npm ci          # Node >=26; CI and Docker use 26.13.1
npm test        # builds, then node --test on the files listed in package.json
npm run build   # tsc -p tsconfig.json
```

CI job `whatsapp-bridge` in `../../../.github/workflows/engine-ci.yml` runs all three. The Dockerfile `whatsapp` stage runs `npm ci`, `npm test && npm run build`, then `npm prune --omit=dev`. Test map: `../../tests/AGENTS.md`.

## Do not

- Widen intake to groups, history, quoted or forwarded content; bypass the bounded original self-chat media path; add unrelated network or disk access; or log secrets.
- Change a frame limit or field on one side only.
- Drop `strict`, `exactOptionalPropertyTypes` or `noUncheckedIndexedAccess`; keep `.js` import suffixes (NodeNext).
