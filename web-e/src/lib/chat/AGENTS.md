# chat/ - transport, submissions and transcript reconciliation

Scope: socket attempts, wire validation, submission identity, history merging and rejected-turn recovery.

## Files

- `attachment-submission.ts`: `createAttachmentSubmission`, immutable upload-to-submission conversion.
- `message.ts`: `ChatAttachment`, `ChatMessage`, `getChatMessageText`; message shape and attachment-aware display text.
- `protocol.ts`: outbound types, `inboundSchema`, inbound/receipt types and `isTurnAccepted`/`isTurnRejected` guards.
- `sessions.ts`: `isWritableSession`, `controlSessionId`; chat/control session eligibility and fallback selection.
- `submissions.ts`: `createChatSubmissions`, `ChatSubmissions`; frozen envelopes, pending/accepted tracking and canonical session identity.
- `transcript.ts`: `ChatLine`, `acceptSubmission`, `mergeServerHistory`, `appendCompletion`; optimistic/history reconciliation.
- `turn-rejection.ts`: `recoverRejectedTurn`, `restoreComposerDraft`; rejected-row state, recovery notices and draft restoration.
- `ws-client.ts`: `connectAgentChat`, `ChatSocket`, `ChatTransport`; socket lifecycle and protocol guard/type re-exports.

## Invariants

- Default ticket chain starts in `AgentChatPage` (`src/pages/app/agent-chat.tsx`):
  `connectAgentChat` -> internal `open` -> `issueChatTicket` (`src/lib/auth-api.ts`) -> `apiRequest` (`src/lib/http.ts`).
  The HTTP POST is `/api/auth/chat-ticket`; body carries `agent_id` and the known submission `session_id` when present.
  Issuance receives the captured owner and abort signal; response requires nonempty `ticket` and `session_id`.
  Resolve the ledger to the returned session ID before building `URLSearchParams` with `ticket`, `session_id`, `lang`.
  Pass `/ws/chat/${agentId}?${params.toString()}` to `transport.url`, then `transport.connect`.
  `handlers.issueTicket` can replace the default issuer; the socket module does not implement the HTTP mint itself.
- Close codes 4001/4003 stop retries and call `onClose`; only 4001 calls `store.expire(owner)`.
  Issuance `ApiError` 401/403 maps to those codes; other issuance failures map to 1011.
  Other closes back off with `Math.min(8000, 600 * 2 ** attempt)`; opening resets the attempt counter.
  `onReconnect` runs on successful openings after the first, not on the initial open.
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
- History merging matches user rows by submission/message ID, not text; deduplicate authoritative rows by `entry_id`.
  Preserve order: older local rows, authoritative history, unmatched pending users, remaining live rows.
  Completion deduplication respects entry IDs and already-authoritative parent message IDs.
- Recover only a matching pending, not-accepted user row; an ID-less rejection produces an onboarding notice without editing rows.
  Invalid submission IDs, submission conflicts and invalid attachments disable retry; other rejection reasons permit it.
  Restore composer text before message content; `restoreComposerDraft` prepends recovered text with a blank-line separator.
- `isWritableSession` accepts existing `shared`/`agent` sessions or sessions with `is_group === true`.
  `controlSessionId` keeps a writable requested ID, otherwise selects the first shared session, otherwise returns `undefined`.

## Gotchas

- `close()` aborts issuance, clears socket queues/timers and unsubscribes; it does not retire a supplied submission ledger.
- Ledger rejection removes pending status but retains the original envelope; retrying that ID reuses its original payload.
- Without a completion message ID, only an identical final ID-less role/content pair suppresses an append.
