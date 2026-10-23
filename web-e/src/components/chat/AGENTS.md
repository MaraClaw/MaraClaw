# chat/ - message and attachment rendering

Scope: message bodies, attachment dialogs, native download controls, local preview renderers.

## Files

- `attachment-card.tsx`: `AttachmentCard`, attachment name/actions and preview dialog shell.
- `attachment-download.tsx`: `AttachmentDownload`, guarded ticket-to-anchor download action.
- `attachment-image.tsx`: `AttachmentImage`, Blob-backed image loading/error display.
- `attachment-office.css`: Office canvas containment, button hit areas, focus treatment.
- `attachment-office.tsx`: `AttachmentOffice`, viewer lifetime, resize fitting, page/slide/sheet navigation.
- `attachment-pdf.tsx`: `AttachmentPdf`, local PDF worker, single-page display and pagination.
- `attachment-preview.tsx`: `AttachmentPreviewBody`, request attempts, renderer dispatch and render-error boundary.
- `markdown.tsx`: `ChatMarkdown`, limited escaped markup rendering.
- `message-content.tsx`: `ChatMessageContent`, message text and attachment-card composition.
- `office-viewer.ts`: `createOfficeViewer` and `NavigableOfficeViewer`, uniform Office adapter.

## Invariants

- Preserve library ownership at the existing import seams:
  card/message-content use `@/lib/chat/message`; download uses `@/lib/workspace-api` and `@/lib/http`;
  image/preview use `@/lib/attachment-preview-api`, preview also uses `@/lib/http`;
  attachment-office/office-viewer use `@/lib/office-preview`; markdown/PDF import no `lib` modules.
- `ChatMessageContent` normalizes text with `getChatMessageText`; only assistant text uses `ChatMarkdown`.
  Markdown escapes source before substitutions; links match HTTP(S), open a new tab with `noreferrer`.
  Supported blocks: fenced code, heading markers, unordered lists, paragraphs; inline code and bold.
- `AttachmentPreviewBody` retries by remounting `PreviewAttempt`; cleanup aborts its request.
  Publish results only while the signal and workspace scope remain current.
  The 20 MiB cap comes from `MAX_PREVIEW_BYTES` in `src/lib/http.ts`, checked against metadata and streamed bytes.
  `BinaryLimitError` and HTTP 413 share the limit message; renderer choice comes from the preview client.
- PDF/Office component chunks load through `lazy` and `Suspense`; Office adapters dynamically import by format.
  Image Blob creation, PDF worker input and Office loading use byte copies, not the shared input buffer.
  Image cleanup removes `src` and revokes its object URL.
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
