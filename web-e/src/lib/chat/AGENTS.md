# chat/ - transport, submissions and transcript reconciliation

Scope: socket attempts, wire validation, submission identity, history merging, skill-mention submission and rejected-turn recovery.

## Files

- `attachment-submission.ts`: `createAttachmentSubmission`, immutable upload-to-submission conversion.
- `history.ts`: `ChatHistory`, `joinHistoryRanges`, `refreshHistory`, `prependHistory`; chronological merge of cursor-paged history by entry identity.
- `live-stream.ts`: live reply correlation by gateway `turn_id`, monotonic `seq`, replacement snapshots and the last 64 completed turns. `done.message_id` remains the assistant entry identifier; a foreign completion must not clear another active stream.
- `message.ts`: `ChatAttachment`, `ChatMessage`, `getChatMessageText`; message shape and attachment-aware display text.
- `protocol.ts`: outbound types, `inboundSchema`, inbound/receipt types and `isTurnAccepted`/`isTurnRejected` guards.
- `sessions.ts`: `isWritableSession`, `controlSessionId`; chat/control session eligibility and fallback selection.
- `skill-mentions.ts`: `resolveSkillMentionSegments`, `findSkillMention`, `filterSkillMentions`, `insertSkillMention`, `buildSkillMentionContent`; installed-skill mention parsing and submission content.
- `submissions.ts`: `createChatSubmissions`, `ChatSubmissions`; frozen envelopes, pending/accepted tracking and canonical session identity.
- `transcript.ts`: `ChatLine`, `acceptSubmission`, `mergeServerHistory`, `appendCompletion`; optimistic/history reconciliation.
- `turn-rejection.ts`: `recoverRejectedTurn`, `restoreComposerDraft`; rejected-row state, recovery notices and draft restoration.
- `ws-client.ts`: `connectAgentChat`, `ChatSocket`, `ChatTransport`, `ChatConnectionState`; socket lifecycle, liveness, offline/suspended handling and protocol guard/type re-exports.

## Invariants

- Default ticket chain starts in `AgentChatPage` (`src/pages/app/agent-chat.tsx`):
  `connectAgentChat` -> internal `open` -> `issueChatTicket` (`src/lib/auth-api.ts`) -> `apiRequest` (`src/lib/http.ts`).
  The HTTP POST is `/api/auth/chat-ticket`; body carries `agent_id` and the known submission `session_id` when present.
  Issuance receives the captured owner and abort signal; response requires nonempty `ticket` and `session_id`.
  Resolve the ledger to the returned session ID before building `URLSearchParams` with `ticket`, `session_id`, `lang` and `heartbeat=1`.
  Pass `/ws/chat/${agentId}?${params.toString()}` to `transport.url`, then `transport.connect`.
  `handlers.issueTicket` can replace the default issuer; the socket module does not implement the HTTP mint itself.
- Close codes 4001/4003 stop retries and call `onClose`; only 4001 calls `store.expire(owner)`.
  Issuance `ApiError` 401/403 maps to those codes; other issuance failures map to 1011.
  Other closes back off with the capped delay `Math.min(8000, 600 * 2 ** attempt)` scaled by `0.5 + 0.5 * random()`; `handlers.random` injects the random source (default `Math.random`; tests pass a constant).
  Only the `connected` event resets the attempt counter, never the socket opening: a server that accepts and then closes before the handshake completes keeps backing off.
  `onReconnect` runs on successful openings after the first, not on the initial open. The engine closes local sockets with 1012 after a Redis resubscribe, which takes this reconnect path and refetches history; there is no resync event.
- Liveness: heartbeat frames count as life and never reach `onEvent`. After `connected`, a socket with no frame for 25 s is discarded and reconnects through the backoff without waiting for `onclose`. The decision comes from the last-frame time, not from timer counts; a timer that fires more than 1 s late (throttled or sleeping tab) gets one 1 s re-check first.
- `CONNECT_DEADLINE_MS` (15 s, capped to what is left of the retry window) bounds each attempt: one that has not received `connected` (a hung ticket request or a silent socket) is discarded and goes through the same backoff. `connected` clears it.
- Retries stop after 120 s of continuous failure, counted from the last healthy moment or the last resume, and each backoff delay is capped to what is left of that window. The state becomes `suspended`: no timers and no ticket requests; the subscription, ledger and queued onboarding triggers are kept. `online`, a visible tab, `send` (when suspended) and `retry()` resume it, and the first two also cut a pending backoff short; pending work is resent once on the next open and `onReconnect` behaves as before. While offline no ticket is requested and the attempt, socket and timers are dropped.
- `onConnectionState` reports `connecting` (first attempt only), `connected`, `reconnecting`, `offline` or `suspended`, on change only. `now`, `schedule`, `online` and `visible` are injectable. 4001 and 4003 behave as above.
- `agent-chat.tsx` releases a live turn whose `done` was lost only when the authoritative history ends with an assistant row, no row is pending, the last user/assistant line is an assistant, and no applied chunk has arrived for 15 s. It calls `liveStore.clear(turnId)` with that turn's id, so later stragglers from it are ignored. A shared or trigger turn that keeps streaming refreshes the quiet clock and is never released; dropping the `turnId === null` guard in the history effect would end such turns early.
- Parse inbound JSON with `inboundSchema.safeParse`; invalid JSON/schema emits `Malformed chat event` as an error event.
  Reserved acceptance/rejection types cannot fall through the generic event schema.
  Acceptance requires UUID submission/message/session IDs; submission rejection requires a UUID submission ID.
  ID-less onboarding rejections exclude invalid-submission, submission-conflict and invalid-attachment reasons.
- Submission matching uses owner object identity, agent ID, and original or canonical session ID.
  Retain the first frozen envelope per submission ID; accepted IDs cannot be submitted again.
  Acceptance requires a known envelope and the canonical session; history reconciliation needs user/submission/message IDs.
  On open, resend ledger-pending submissions before queued onboarding triggers; owner retirement retires the ledger and closes.
- Attachment submissions generate a UUID, use extracted text or `[Uploaded ${filename}]`, and retain workspace path/name.
  `display_content` and `file_name` hold the filename; freeze both attachment records and their array.
  `getChatMessageText` strips a matching user `[file:name]\n` prefix and hides filename-only attachment text.
- Paged history ranges are chronological. `joinHistoryRanges(earlier, later)` keys rows by `entry_id` and prefers the newer range's row on overlap, never merging on a shared parent `message_id`, because a parent's projected siblings can span two pages.
  `refreshHistory` joins the latest window onto the retained items and keeps the retained cursor; `prependHistory` takes the older page's cursor and `has_more` and joins it ahead of the retained items.
- Transcript merging matches user rows by submission/message ID, not text; deduplicate authoritative rows by `entry_id`.
  Preserve order: older local rows, authoritative history, unmatched pending users, remaining live rows.
  Completion deduplication respects entry IDs and already-authoritative parent message IDs.
- A mention must start the message or follow whitespace; only names matching an installed skill resolve, and a trailing `.` run is tolerated. The picker and chips share `resolveSkillMentionSegments`, so display and submission cannot disagree.
  `buildSkillMentionContent` trims the draft. With no resolved skill, `content` is just that text; otherwise `content` lists each distinct skill's exact workspace-relative path ahead of the request and `display_content` keeps the user's text.
- Recover only a matching pending, not-accepted user row; an ID-less rejection produces an onboarding notice without editing rows.
  Invalid submission IDs, submission conflicts and invalid attachments disable retry; other rejection reasons permit it.
  Restore composer text before message content; `restoreComposerDraft` prepends recovered text with a blank-line separator.
- `isWritableSession` accepts existing `shared`/`agent` sessions or sessions with `is_group === true`.
  `controlSessionId` keeps a writable requested ID, otherwise selects the first shared session, otherwise returns `undefined`.

## Gotchas

- `close()` aborts issuance, clears socket queues, the retry, liveness and connect-deadline timers, and unsubscribes from the session store and the online and visibility signals; it does not retire a supplied submission ledger.
- Ledger rejection removes pending status but retains the original envelope; retrying that ID reuses its original payload.
- Without a completion message ID, only an identical final ID-less role/content pair suppresses an append.
