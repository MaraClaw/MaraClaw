# connectors/zalo

Private Node 26 ESM package `@maraclaw/zalo-bridge`, with its own lockfile and pinned `zca-js` 2.3.0. One subprocess owns one personal Zalo account and live listener. Python owns durable credentials, the exclusive account lease, sender authorization, inbox deduplication and retry policy. Operational setup: `../../docs/channels.md`; detailed wire contract: `README.md`.

## Where to look

| Concern | Source |
|---|---|
| Process entry and framed stdio | `src/bridge.ts`, `src/supervisor.ts` |
| Worker heap ceiling | `src/limits.ts` (`workerLimits`), applied as `resourceLimits` in `src/bridge.ts` |
| Worker entry and lifecycle | `src/worker.ts`, `src/runtime.ts` |
| Strict schemas, byte framing and persistence ACK | `src/protocol.ts`, `src/peer.ts` |
| Typed SDK seam, QR/restore, listener and receipts | `src/sdk.ts` (`SdkClient`, `SdkApi`, `SdkAdapter`) |
| Send ledger, keepalive and typing | `src/sends.ts`, `src/heartbeat.ts`, `src/typing.ts` |
| Bounded private files | `src/media_protocol.ts`, `src/media_transport.ts`, `src/media_download.ts`, `src/media_offers.ts`, `src/media_attachment.ts` |
| Python process owner and protocol mirror | `../../app/services/channels/zalo_personal_supervisor.py`, `../../app/services/channels/zalo_personal_protocol.py` |
| Persistence ordering examples | `test/login.test.ts`; file contracts: `test/media.test.ts` |

## Process and protocol

- The Python supervisor spawns `node dist/src/bridge.js`. The main thread owns stdin/stdout framing, token checks, lease enforcement and worker termination; the SDK runs only in the worker. Worker stdout/stderr are captured and discarded.
- Keep the independent shutdown watchdog: stop revokes intake and closes the listener; an unresponsive worker is terminated after 2 seconds, with a further 1-second process exit bound. EOF, SIGINT and SIGTERM enter shutdown.
- Heap ceiling: the parent appends `--worker-heap-mb=<MiB>` to the command (not an `init` field, because the worker exists before `init`); `workerLimits` turns it into the worker's `maxOldGenerationSizeMb`, no flag keeps Node's defaults and a malformed one aborts startup. A worker that runs out of memory raises `error`/`exit` on the main thread, which closes with `transport_failed` like any dead worker, so Python charges the durable retry budget; the fixture bridge in `test/fixtures/` (mode `oom`) covers it.
- `src/protocol.ts` uses strict Zod schemas in both directions. Frames are UTF-8 NDJSON, capped at 1 MiB excluding LF before decoding/parsing, including unterminated input. Unknown keys, invalid UTF-8 and malformed frames close the bridge.
- Require exactly one initial `init` within 15 seconds. Every frame carries `id` and a per-run `token`; later tokens use constant-time comparison. IDs are 1..256 code units, tokens 1..512, provider IDs 1..64 decimal digits, and `lease_ms` is 1..120000.
- Outgoing text is nonblank and capped at 2000 Unicode code points; incoming text is capped at 131072 UTF-16 code units. Attachment sends use the separate staged-transfer contract in `README.md`. Change Python and TypeScript schemas together.

## Credentials and intake

- Preserve `src/runtime.ts` login/refresh ordering: resolved API credentials go through `creds_set`, and Python must durably store them before returning `result` with the exact `request_id` and `ok: true`. Only then emit identity, start the listener, announce readiness on `connected`, and accept intake. Restored logins use the same barrier.
- `Peer.persist` allows one commit, waits 15 seconds, and fails closed on NACK/timeout. Unknown or repeated ACKs are protocol errors. Refresh re-reads the cookie jar after each ACK; intake and new sends/typing remain gated while credentials are committing.
- QR data is canonical base64 PNG, expires after 100 seconds, and never becomes a credential snapshot before login resolves. The bridge writes neither QR images nor credentials to disk.
- Only live private user messages with supported original text/media enter the bridge. Groups, self messages and history are excluded. Python authorizes the bound sender; contacts and display names do not establish access.
- Restore failures do not trigger QR fallback or an SDK reconnect loop. Invalid stored data is `login_failed`; unclassified SDK restore/listener errors are transport failures, not evidence of credential revocation. Python owns recovery.
- Keepalive calls never overlap. Changed credentials use the same durable ACK barrier before further heartbeat scheduling or new provider operations. See `README.md` for cadence, timeout and typing limits.
- Identical command-ID replays reuse the original send result. Never retry an uncertain send; worker death leaves commands without terminal results uncertain. Provider acknowledgment is not recipient-delivery proof.

## Commands and local conventions

```bash
npm ci --ignore-scripts  # README setup, from this package
npm test                # builds, then node --test dist/test/*.test.js
npm run build           # tsc -p tsconfig.json
npm start               # built bridge on dedicated stdin/stdout pipes
```

`../../../.github/workflows/engine-ci.yml` has a separate `zalo-bridge` job on Node 26.13.1 (`npm ci`, `npm test`). The `zalo` stage in `../../Dockerfile` builds through `npm test` before pruning dev dependencies. Tests exercise the typed SDK seam and subprocess pipes without a live Zalo account.

- Keep `.js` import suffixes, NodeNext resolution and strict compiler flags. `tsconfig.json` maps `zca-js` to its shipped `dist/index.d.ts` because the package root declaration has an extensionless directory export; emitted runtime imports remain `zca-js`.
- Do not log raw frames, QR data, cookies, credentials, tokens, message bodies or SDK error/event payloads. Never write banners or diagnostics to stdout or move SDK execution into the main thread.
- Do not bypass the startup/refresh persistence barrier, infer delivery from process exit, or infer sender authorization from provider contacts. This is a personal-account bridge; the Zalo Official Account API is a separate integration.
