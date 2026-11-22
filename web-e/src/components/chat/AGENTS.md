# chat/ - message and attachment rendering

Scope: windowed transcript and live reply rendering, response copy, attachment dialogs, local previews, history status and the skill-mention composer.

## Files

- `attachment-card.tsx`: `AttachmentCard`, attachment name/actions and preview dialog shell.
- `attachment-download.tsx`: `AttachmentDownload`, guarded ticket-to-anchor download action.
- `attachment-image.tsx`: `AttachmentImage`, Blob-backed image loading/error display.
- `attachment-office.css`: Office canvas containment, button hit areas, focus treatment.
- `attachment-office.tsx`: `AttachmentOffice`, viewer lifetime, resize fitting, page/slide/sheet navigation.
- `attachment-pdf.tsx`: `AttachmentPdf`, local PDF worker, single-page display and pagination.
- `attachment-preview.tsx`: `AttachmentPreviewBody`, request attempts, renderer dispatch and render-error boundary.
- `attachment-video.tsx`: `AttachmentVideo`, copied-Blob playback and element/object URL cleanup.
- `history-control.tsx`: `HistoryControl`, conversation loading/error status and the earlier-messages control.
- `live-reply.tsx`: `LiveReply`, external live-stream subscription to the published snapshot (one per animation frame); copy uses the displayed text and the scroll-update callback runs when the snapshot changes.
- `markdown-blocks.ts`: `splitMarkdownBlocks`, `createMarkdownSplitter`, `parseMarkdownTable`; fenced blocks and pipe tables, including escaped/code-span pipes. The splitter gives the same blocks as `splitMarkdownBlocks` for any chunking; leading blocks no later text can change are `completed` and not rescanned while the text only grows, and a replacement reuses only the prefix whose deciding lines are unchanged.
- `markdown.tsx`: `ChatMarkdown`, limited escaped markup rendering; one splitter per instance and memoized blocks, so completed blocks are not rendered again as the tail grows.
- `message-content.tsx`: `ChatMessageContent`, message text and attachment-card composition.
- `office-viewer.ts`: `createOfficeViewer` and `NavigableOfficeViewer`, uniform Office adapter.
- `response-copy.tsx`: `ResponseCopy`, exact source clipboard text and source-bound success/failure feedback.
- `session-list.tsx`: `SessionList`, a collapsed `Other conversations` navigation at the top of the scrolling thread (never a header or sidebar: the chat panel must keep filling the workspace). It lists loaded sessions as links: the caller's own conversation first, labelled `Your conversation` and linking to the default chat route, then other owners by `username` (or title) with a `Read-only` badge when the session is not writable. It offers Load older conversations and renders nothing when only the caller's own conversation is loaded and no further page exists. Data comes from the paged sessions query in `agent-chat.tsx`.
- `skill-mention-input.tsx`: native composer and installed-skill picker; accent chips are an aria-hidden, noninteractive background mirror, never a second editor.
- `transcript.tsx`: `ChatTranscript`, memoized rows, measured wrappers, focus registration, spacer gaps, delivery/retry state and timestamps.

## Invariants

- Chip recognition shares `resolveSkillMentionSegments` with submission resolution. Keep both text layers' metrics, wrapping, padding and scrollbar geometry identical; native textarea owns text, selection, caret and scrolling.
- `HistoryControl` takes state from `useChatHistory` and has no data fetching of its own. Loading shows a status; request failures mount `RequestErrorToast`, which renders no inline alert. Retry conversation remains available; once ready, an earlier-page failure keeps the earlier-page control with Retry earlier messages. With no earlier page it shows Beginning of conversation.
  A pointer click on Load earlier messages disables the button while loading. A keyboard activation (`event.detail === 0`) uses `aria-disabled` instead, so focus is not lost mid-load; blur clears that mode. A polite live region announces earlier-page loading.
- Preserve library ownership at the existing import seams:
  card/message-content use `@/lib/chat/message`; download uses `@/lib/workspace-api` and `@/lib/http`;
  image/video/preview use `@/lib/attachment-preview-api`, preview also uses `@/lib/http`;
  attachment-office/office-viewer use `@/lib/office-preview`; history-control uses `@/lib/http` for `userFacingRequestError`; markdown/PDF import no `lib` modules.
- `ChatMessageContent` normalizes text with `getChatMessageText`; only assistant text uses `ChatMarkdown`.
  Markdown escapes source before substitutions; links match HTTP(S), open a new tab with `noreferrer`.
  Supported blocks: fenced code (including blank lines), heading markers, unordered lists, paragraphs and pipe tables with delimiter alignment; inline code and bold. Tables may follow prose without a blank line and scroll horizontally inside the response. Assistant copy uses exact `message.content`, never rendered text or attachment-aware display normalization.
- `ChatTranscript` consumes the viewport from `useChatScroll`; measured wrappers keep `data-transcript-row`, focus callbacks and spacer heights aligned with the library layout. Row articles retain `data-chat-entry` for scroll anchoring. `LiveReply` subscribes separately and requests a scroll update after rendering.
- `AttachmentPreviewBody` retries by remounting `PreviewAttempt`; cleanup aborts its request.
  Publish results only while the signal and workspace scope remain current.
  The 20 MiB cap comes from `MAX_PREVIEW_BYTES` in `src/lib/http.ts`, checked against metadata and streamed bytes.
  `BinaryLimitError` and HTTP 413 share the limit message; renderer choice comes from the preview client.
- PDF/Office component chunks load through `lazy` and `Suspense`; Office adapters dynamically import by format.
  Image/video Blob creation, PDF worker input and Office loading use byte copies, not the shared input buffer.
  Image cleanup removes `src` and revokes its object URL.
- MP4, WebM and Ogg video MIME types dispatch to `AttachmentVideo`. Cleanup pauses playback, removes `src`, calls `load()` and revokes the object URL; controls use `preload="none"` and `playsInline`.
- PDF worker is `pdfjs-dist/build/pdf.worker.min.mjs`; disable eval, Wasm and annotation rendering.
  Keep text rendering; `maxImageSize` is 16,777,216; observed width clamps to 1-1000 and device pixel ratio to 2.
  Password-protected PDFs show download guidance rather than a password form.
- Office lifetime uses an abort controller and disconnects its resize observer on cleanup.
  `loadOfficeViewer` destroys on abort/failure and enforces a 20,000 ms load deadline.
  Shared Office options use worker mode, 10,000 ms worker timeout, no Google Fonts or hyperlinks.
- Office admission limits: 32 MiB/archive entry, 64 MiB total inflated data, 2,000 entries, 32 MiB decoded images.
  XLSX worksheet options: 100,000 rows, 250,000 cells, 32 MiB owned UTF-8, 64 MiB JSON.
  PPTX disables media playback; DOCX/PPTX wait for layout before fitting width; XLSX fits by relayout.
- Download clicks reject duplicate pending work; request `inline: false`, then click a download anchor.
  Check both mount activity and workspace scope after ticket issuance before navigation or UI feedback.

## Gotchas

- Unsupported preview formats return metadata without fetching bytes; download controls remain available.
- Request failures offer Retry preview; renderer-boundary failures offer download guidance instead.
