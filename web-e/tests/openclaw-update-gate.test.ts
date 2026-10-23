import assert from 'node:assert/strict'
import { test } from 'node:test'
import { matchRoutes } from 'react-router-dom'

import { agentSettingsPath, canMountAgentOutlet, isActiveUpdate, openclawUpdateRoute, updateNoticePath, updateWorkGate } from '../src/lib/openclaw-update-policy.ts'
import { agentId, notice, run, update } from './openclaw-update-fixture.ts'

const chat = `/app/agents/${agentId}/chat/session-1`

for (const input of [
  { update: undefined, error: false, submitting: false },
  { update: undefined, error: true, submitting: false },
  { update: { ...update, required: false }, error: true, submitting: false },
  { update, error: false, submitting: false },
  { update: { ...update, required: false }, error: false, submitting: true },
  { update: { ...update, required: false, run }, error: false, submitting: false },
  { update: { ...update, required: false, run: { ...run, state: 'cutover' as const } }, error: false, submitting: false },
]) {
  test(`work is never mounted while gate is ${updateWorkGate(input)}`, () => {
    // Given
    const gate = updateWorkGate(input)
    // When / Then
    assert.equal(canMountAgentOutlet(chat, gate), false)
    assert.equal(canMountAgentOutlet(`/app/agents/${agentId}/files`, gate), false)
    assert.equal(canMountAgentOutlet(agentSettingsPath(agentId), gate), true)
  })
}

for (const state of ['failed', 'succeeded'] as const) {
  test(`a ${state} run alone never releases a mandatory gate`, () => {
    // Given
    const status = { ...update, run: { ...run, state } }
    // When
    const gate = updateWorkGate({ update: status, error: false, submitting: false })
    // Then
    assert.equal(gate, 'required')
    assert.equal(canMountAgentOutlet(chat, gate), false)
  })
}

test('optional notices permit work until an explicit upgrade starts', () => {
  // Given
  const optional = { ...update, notice: { ...notice, mandatory: false }, required: false }
  // When
  const gate = updateWorkGate({ update: optional, error: false, submitting: false })
  // Then
  assert.equal(canMountAgentOutlet(chat, gate), true)
})

test('authoritative successful completion releases work and ends active polling', () => {
  // Given
  const completed = { ...update, required: false, run: { ...run, state: 'succeeded' as const } }
  // When
  const gate = updateWorkGate({ update: completed, error: false, submitting: false })
  // Then
  assert.equal(canMountAgentOutlet(chat, gate), true)
  assert.equal(isActiveUpdate(completed.run), false)
})

test('notification URLs resolve the same detail route registered by AppRouter', () => {
  // Given
  const routes = [{ path: '/app', children: [{ path: openclawUpdateRoute }] }]
  // When
  const matches = matchRoutes(routes, updateNoticePath(notice.id))
  // Then
  assert.equal(matches?.at(-1)?.params['noticeId'], notice.id)
  assert.equal(matches?.at(-1)?.route.path, openclawUpdateRoute)
  assert.equal(canMountAgentOutlet(`${agentSettingsPath(agentId)}/other`, 'required'), false)
})
