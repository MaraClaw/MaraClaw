# Zalo personal subprocess bridge

Node 26 ESM bridge pinned to `zca-js` **2.3.0**, upstream commit
`d7c12fd0bc3a6296b38979d2791d3dde663771a4`. This uses personal Zalo QR login, not an OA API.
One subprocess owns one account and one live listener. Python owns durable credentials,
the exclusive account lease, sender authorization, inbox deduplication and retry policy.

```sh
npm ci --ignore-scripts
npm test
npm start
```

Invoke `node engine/connectors/zalo/dist/src/bridge.js` after building, with dedicated
stdin/stdout pipes. The main thread owns framing, authentication, lease enforcement and
worker termination. Only the worker imports the SDK. Worker stdout/stderr are captured
and discarded, including unexpected SDK console output and errors. Neither QR images
nor credentials are written to disk. Do not log raw protocol frames in the caller.

`npm test` builds and tests the production SDK adapter through its typed client seam,
plus the real supervisor over subprocess pipes. It never contacts a live Zalo account.
The SDK's published root declaration re-exports an extensionless directory, which is
incompatible with NodeNext. The compiler's `paths` entry resolves to the SDK's own
shipped `dist/index.d.ts`; emitted imports still use the unchanged `zca-js` package.

## Wire contract

UTF-8 NDJSON, exactly one JSON object per line. Both directions are capped at
**1,048,576 bytes excluding LF**, before decoding/parsing. Invalid UTF-8, invalid JSON,
unknown keys, missing fields and out-of-range values close the bridge. No banners or
diagnostics appear on stdout. A final `closed` frame is sent when an init token is known;
EOF before init can exit without a frame. Never infer successful delivery from exit code.

Every frame has mandatory `id: string` (1..256 UTF-16 code units) and `token: string`
(1..512 code units). Generate a fresh cryptographic token for each run. It must match
the init token on every later frame; comparison uses constant-time byte comparison.
Parent IDs identify envelopes. Child IDs are fresh UUIDs; `creds_set.id` is the request
ID that the persistence acknowledgment must reference. `command_id` identifies sends
independently of envelope IDs. Unknown or repeated persistence acknowledgments are fatal.

Provider account, sender and thread IDs are decimal strings of 1..64 digits. Other
correlation/message IDs are nonempty strings of at most 256 code units. `lease_ms` is
an integer in **1..120000**. Outgoing `text` is nonblank, at most **2000 Unicode code
points** (and at most 4000 UTF-16 code units). Python must split outgoing text at the
same 2000-code-point boundary. Incoming text is preserved up to 131072 UTF-16 code units;
invalid or oversized provider messages are discarded.

All schemas below are strict: the displayed fields plus `id` and `token` are the entire
frame. No protocol extensions were added.

### Parent to child

```ts
{ id, token, type: "init", lease_ms: number, credentials: string | null }
{ id, token, type: "lease", lease_ms: number }
{ id, token, type: "shutdown" }
{ id, token, type: "result", request_id: string, ok: boolean }
{ id, token, type: "send", command_id: string, thread_id: string, text: string }
```

Send init first, within 15 seconds of launch, exactly once. `credentials: null` requests
`Zalo.loginQR({}, callback)`. A nonnull string (at most 768000 code units) must contain
the opaque JSON previously returned in `creds_set`. It is parsed as:

```ts
{
  cookie: Array<{ key: string, value: string, domain: string, ...cookieJsonFields }>,
  imei: string,
  userAgent: string
}
```

The top-level credentials object is strict. Cookie entries preserve JSON cookie fields;
`key` and `domain` must be nonempty, `value` must be a string. There must be 1..512 cookies.
`imei` is 1..512 code units and `userAgent` 1..4096. Restore calls `Zalo.login(credentials)`
once. Any invalid/failed restore closes with `login_failed`; the caller must explicitly
relink in a fresh run. There is no QR fallback or reconnect loop.

Renew the lease before expiry; each lease replaces its deadline relative to receipt.
Lease renewal does not extend QR expiry, credential acknowledgment or login deadlines.
The parent must stop renewing when it loses its database lease. Shutdown, stdin EOF,
SIGINT and SIGTERM revoke intake and close the listener. An independent main-thread
watchdog terminates even an unresponsive worker after 2 seconds, with a final process
exit bound of 1 further second. A parent should still bound its own process wait/kill.

### Child to parent

```ts
{ id, token, type: "qr", value: string, expires_at: string }
{ id, token, type: "creds_set", creds: string }
{ id, token, type: "identity", account_id: string }
{ id, token, type: "ready" }
{ id, token, type: "message", message_id: string, sender_id: string,
  thread_id: string, from_me: boolean, timestamp: number, text: string }
{ id, token, type: "send_result", command_id: string, message_id: string,
  outcome: "accepted" | "rejected" | "uncertain" }
{ id, token, type: "closed", reason:
  "shutdown" | "lease_expired" | "protocol_error" | "login_failed" |
  "auth_store_failed" | "transport_failed" | "qr_expired" | "qr_declined" }
```

`qr.value` is canonical base64 PNG (maximum 768000 code units), with the PNG signature
and no data-URL prefix. It is the SDK's `QRCodeGenerated.data.image`, never the raw QR
code or SDK token. `expires_at` is UTC ISO 8601, 100 seconds after generation, matching
the pinned SDK's local lifetime. Generated callbacks replace the displayed image.
Clear the displayed QR on `closed`, `identity`, or the advertised expiry, whichever is
first. SDK expiry/decline events abort and close the run; a local timer also enforces
expiry when the callback does not arrive. There is no automatic regeneration.

Only after `loginQR`/`login` resolves does the bridge serialize the resolved API context's
`cookie.toJSON().cookies`, `imei` and `userAgent`. `GotLoginInfo` does not persist anything.
The `creds_set.creds` string is at most 768000 code units and must fit the byte frame cap.
**Durably store it before replying** with `result.request_id = creds_set.id, ok = true`.
The bridge waits up to 15 seconds for that exact acknowledgment. `ok = false` or timeout
closes with `auth_store_failed`. Neither identity nor listener startup nor intake can
precede successful acknowledgment, including restored logins.

Then `identity.account_id` comes from synchronous, authoritative `api.getOwnId()`.
The listener starts with `retryOnClose: false`; `ready` means its `connected` event fired.
Login through listener connection has a 120-second deadline. Intake accepts only live
`ThreadType.User` messages with `msgType = "webchat"`, nonblank string content and a
valid millisecond timestamp. Groups, media, self messages and history are excluded.
`sender_id` is `data.uidFrom`, `thread_id` is the SDK thread ID, `from_me` is always false,
and `timestamp` is integer Unix **seconds**, converted from SDK milliseconds. Python
must authorize the bound sender; neither contacts nor display names establish ownership.
The SDK has no provider acknowledgment/replay for receive, so this is live intake only.

Sends call `api.sendMessage(text, thread_id, ThreadType.User)` exactly once per new
command. `accepted` requires a nonnull `response.message.msgId`, converted to a decimal
string without accepting an unsafe JS integer; SDK big-integer JSON values preserve
their decimal representation. Attachment acknowledgments do not count. Missing message
acknowledgment, an exception after dispatch, a 15-second send deadline or disconnect
produces `uncertain`, with `message_id: ""`. `rejected` means no SDK send was dispatched
(not ready or 32 SDK calls still in flight), also with an empty message ID. Timed-out
SDK calls retain their in-flight slots until their promises actually settle. Accepted means
SDK acceptance, not proof the recipient read the message.

Identical command-ID replays do not dispatch again: pending replays await the original
result; completed replays repeat that result. Reusing a command ID with different text
or thread closes with `protocol_error`; pending sends become uncertain. The ledger
holds at most 10000 commands per run and then closes rather than evicting deduplication
history. It is intentionally not durable across runs. Malformed send frames fail schema
validation and close with `protocol_error`; they never reach the SDK. The bridge never
retries an uncertain send. If a worker dies or must be forcibly terminated, the parent
must classify all commands without a terminal result as uncertain, even if only `closed`
is received.

## Pinned source evidence

- [QR callback, PNG and 100-second timer](https://github.com/RFS-ADRENO/zca-js/blob/d7c12fd0bc3a6296b38979d2791d3dde663771a4/src/apis/loginQR.ts)
- [QR/credential login](https://github.com/RFS-ADRENO/zca-js/blob/d7c12fd0bc3a6296b38979d2791d3dde663771a4/src/zalo.ts)
- [Authoritative identity](https://github.com/RFS-ADRENO/zca-js/blob/d7c12fd0bc3a6296b38979d2791d3dde663771a4/src/apis/getOwnId.ts)
- [Listener lifecycle and live message dispatch](https://github.com/RFS-ADRENO/zca-js/blob/d7c12fd0bc3a6296b38979d2791d3dde663771a4/src/apis/listen.ts)
- [Send acknowledgment shape](https://github.com/RFS-ADRENO/zca-js/blob/d7c12fd0bc3a6296b38979d2791d3dde663771a4/src/apis/sendMessage.ts)
