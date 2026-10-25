import assert from 'node:assert/strict'
import { test } from 'node:test'
import { inboundSchema } from '../src/lib/chat/protocol.ts'
import { canonicalMessage, canonicalSession, firstSubmission } from './socket-fixture.ts'

test('completion wire payloads preserve explicitly nullable fields', () => {
  // Given
  const payload = {
    type: 'done', content: null, role: null, session_id: canonicalSession,
    message_id: canonicalMessage, entry_id: null, client_submission_id: firstSubmission, attachments: null,
  }
  // When
  const result = inboundSchema.safeParse(payload)
  // Then
  assert.equal(result.success, true)
  if (result.success) assert.deepEqual(result.data, payload)
})

test('tool-call wire payloads retain structured arguments and results', () => {
  // Given
  const payload = {
    type: 'tool_call', name: 'lookup', status: 'done', arguments: { query: 'contract' },
    result: { found: true, values: [1, null, 'match'] }, live_preview: null,
  }
  // When
  const result = inboundSchema.safeParse(payload)
  // Then
  assert.equal(result.success, true)
  if (result.success) assert.deepEqual(result.data, payload)
})

test('correlated rejection wire payloads preserve a nullable notice identifier', () => {
  // Given
  const payload = {
    type: 'turn_rejected', reason: 'quota_exceeded', content: 'Quota exceeded',
    client_submission_id: firstSubmission, notice_id: null,
  }
  // When
  const result = inboundSchema.safeParse(payload)
  // Then
  assert.equal(result.success, true)
  if (result.success) assert.deepEqual(result.data, payload)
})
