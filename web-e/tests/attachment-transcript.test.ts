import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createAttachmentSubmission } from '../src/lib/chat/attachment-submission.ts'
import { getChatMessageText } from '../src/lib/chat/message.ts'
import { inboundSchema } from '../src/lib/chat/protocol.ts'
import { appendCompletion, mergeServerHistory, type ChatLine } from '../src/lib/chat/transcript.ts'

const uploaded = { workspace_path: 'uploads/opaque/report.pdf', filename: 'Quarterly report.pdf', extracted_text: 'Extracted document for the model' }
const attachments = [{ path: uploaded.workspace_path, name: uploaded.filename }]
const savedContent = '[file:Quarterly report.pdf]\nQuarterly report.pdf'

test('upload submission carries the workspace path separately from model content and display name', () => {
  // Given / When
  const submission = createAttachmentSubmission(uploaded)
  // Then
  assert.deepEqual(submission.attachments, [{ path: 'uploads/opaque/report.pdf', name: 'Quarterly report.pdf' }])
  assert.equal(submission.content, uploaded.extracted_text)
  assert.equal(submission.display_content, uploaded.filename)
})

test('authoritative history preserves attachment references when replacing the pending upload', () => {
  // Given
  const submission = createAttachmentSubmission(uploaded)
  const pending: ChatLine = { ...submission, content: uploaded.filename, role: 'user', pending: true, submission }
  const persisted: ChatLine = {
    role: 'user', content: savedContent, message_id: 'saved', client_submission_id: submission.client_submission_id, attachments,
  }
  // When
  const history = mergeServerHistory([persisted], [pending])
  // Then
  assert.equal(history.length, 1)
  assert.equal(history[0]?.pending, undefined)
  assert.deepEqual(history[0]?.attachments, [{ path: 'uploads/opaque/report.pdf', name: 'Quarterly report.pdf' }])
  assert.deepEqual(history.map(getChatMessageText), [''])
})

test('a fresh transcript displays attachment references using server history alone', () => {
  // Given
  const history = [{ role: 'user', content: savedContent, message_id: 'saved', attachments }]
  // When
  const reopened = mergeServerHistory(history, [])
  // Then
  assert.deepEqual(reopened[0]?.attachments, [{ path: 'uploads/opaque/report.pdf', name: 'Quarterly report.pdf' }])
  assert.deepEqual(reopened.map(getChatMessageText), [''])
})

test('a pending upload displays only its attachment card', () => {
  // Given
  const submission = createAttachmentSubmission(uploaded)
  const pending: ChatLine = { ...submission, content: uploaded.filename, role: 'user', pending: true }
  // When
  const text = getChatMessageText(pending)
  // Then
  assert.equal(text, '')
  assert.deepEqual(pending.attachments, attachments)
})

test('history refetch keeps a single attachment card without the stored marker or duplicate filename', () => {
  // Given
  const persisted = { role: 'user', content: savedContent, message_id: 'saved', attachments }
  const local = mergeServerHistory([persisted], [])
  // When
  const refreshed = mergeServerHistory([persisted], local)
  // Then
  assert.equal(refreshed.length, 1)
  assert.deepEqual(refreshed.flatMap((row) => row.attachments ?? []), attachments)
  assert.deepEqual(refreshed.map(getChatMessageText), [''])
})

test('exact attached leading marker normalization preserves accompanying user prose through history', () => {
  // Given
  const prose = '  Please review Quarterly report.pdf.\nKeep this text.\n'
  const persisted = { role: 'user', content: `[file:Quarterly report.pdf]\n${prose}`, message_id: 'saved', attachments }
  // When
  const history = mergeServerHistory([persisted], []).map(getChatMessageText)
  // Then
  assert.deepEqual(history, [prose])
})

for (const content of [
  '[file:other.pdf]\nQuarterly report.pdf',
  ' [file:Quarterly report.pdf]\nQuarterly report.pdf',
  'Please review\n[file:Quarterly report.pdf]\nQuarterly report.pdf',
  '[file:Quarterly report.pdf]Quarterly report.pdf',
  'Quarterly report.pdf\n',
]) {
  test(`attachment text normalization preserves unrelated content: ${JSON.stringify(content)}`, () => {
    // Given
    const message = { role: 'user', content, attachments }
    // When
    const text = getChatMessageText(message)
    // Then
    assert.equal(text, content)
  })
}

test('attachment normalization removes only one exact leading marker', () => {
  // Given
  const message = { role: 'user', content: `[file:Quarterly report.pdf]\n${savedContent}`, attachments }
  // When
  const text = getChatMessageText(message)
  // Then
  assert.equal(text, savedContent)
})

for (const message of [
  { role: 'user', content: savedContent },
  { role: 'assistant', content: savedContent, attachments },
  { role: 'assistant', content: '**Quarterly report.pdf**\n[file:Quarterly report.pdf]', attachments },
  { role: 'assistant', content: uploaded.filename, attachments },
]) {
  test(`non-attached user text and assistant markup remain unchanged: ${JSON.stringify(message)}`, () => {
    // Given / When
    const text = getChatMessageText(message)
    // Then
    assert.equal(text, message.content)
  })
}

for (const { content, text } of [
  { content: savedContent, text: '' },
  { content: '[file:Quarterly report.pdf]\nPlease review this report.', text: 'Please review this report.' },
]) {
  test(`shared done retains attachment cards and text before history refetch: ${JSON.stringify(content)}`, () => {
    // Given
    const frame = { type: 'done', role: 'user', content, message_id: 'shared', attachments }
    // When
    const event = inboundSchema.parse(frame)
    const completed = appendCompletion([], {
      role: event.role ?? 'assistant', content: event.content ?? '',
      ...(event.attachments === undefined ? {} : { attachments: event.attachments }),
      ...(event.message_id === undefined ? {} : { message_id: event.message_id }),
    })
    // Then
    assert.deepEqual(completed[0]?.attachments, attachments)
    assert.deepEqual(completed.map(getChatMessageText), [text])
  })
}

for (const malformed of [
  null, {}, 'report.pdf', [null], [42], [{}],
  [{ path: uploaded.workspace_path }], [{ name: uploaded.filename }],
  [{ path: 42, name: uploaded.filename }], [{ path: uploaded.workspace_path, name: 42 }],
  [{ path: '', name: uploaded.filename }], [{ path: uploaded.workspace_path, name: '' }],
]) {
  test(`done parser rejects malformed attachment references: ${JSON.stringify(malformed)}`, () => {
    // Given
    const frame = { type: 'done', role: 'user', content: savedContent, attachments: malformed }
    // When
    const parsed = inboundSchema.safeParse(frame)
    // Then
    assert.equal(parsed.success, false)
  })
}
