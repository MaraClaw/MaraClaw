import assert from 'node:assert/strict'
import { test } from 'node:test'
import { controlSessionId, isWritableSession } from '../src/lib/chat/sessions.ts'

const archive = { id: 'archive', source_channel: 'web', is_primary: true, user_id: 'viewer' }
const shared = { id: 'shared', source_channel: 'shared', is_primary: true, user_id: 'creator' }
const peer = { id: 'peer', source_channel: 'agent' }
const group = { id: 'group', source_channel: 'feishu', is_group: true }

test('direct control selects shared regardless of archive order, primary flag or creator provenance', () => {
  const sessions = [archive, peer, shared]
  const selected = controlSessionId(sessions, '')
  assert.equal(selected, shared.id)
  assert.equal(controlSessionId(sessions, archive.id), shared.id)
})

test('direct control never falls back to a private or peer session when shared is absent', () => {
  const selected = controlSessionId([archive, peer], '')
  assert.equal(selected, undefined)
})

for (const session of [peer, group]) {
  test(`explicit ${session.id} control retains its own scope`, () => {
    const selected = controlSessionId([archive, shared, session], session.id)
    assert.equal(selected, session.id)
  })
}

test('only shared, peer and group sessions accept new turns', () => {
  const writable = [undefined, archive, { ...archive, source_channel: 'slack' }, shared, peer, group].map(isWritableSession)
  assert.deepEqual(writable, [false, false, false, true, true, true])
})
