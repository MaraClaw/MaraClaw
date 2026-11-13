import assert from 'node:assert/strict'
import { test } from 'node:test'

import { hiddenSectionRedirect, visibleAgentTabs } from '../src/lib/agent-section-visibility.ts'

const tabs = [
  { id: 'chat', to: 'chat' },
  { id: 'files', to: 'files' },
  { id: 'activity', to: 'activity' },
  { id: 'people', to: 'relationships' },
  { id: 'settings', to: 'settings' },
] as const
const ids = (items: readonly { id: string }[]) => items.map((tab) => tab.id)

test('Given no hidden sections, when tabs are filtered, then only the manage-only Activity rule applies', () => {
  // Given / When / Then
  assert.deepEqual(ids(visibleAgentTabs(tabs, [], 'use')), ['chat', 'files', 'people', 'settings'])
  assert.deepEqual(ids(visibleAgentTabs(tabs, [], 'manage')), ids(tabs))
})

test('Given hidden section ids, when tabs are filtered, then they are removed by id in the original order', () => {
  // Given
  const hidden = ['files', 'people']
  // When
  const visible = visibleAgentTabs(tabs, hidden, 'manage')
  // Then
  assert.deepEqual(ids(visible), ['chat', 'activity', 'settings'])
})

test('Given a hidden section, when the path is inside it, then it redirects to the first visible tab', () => {
  // Given
  const hidden = ['chat']
  const visible = visibleAgentTabs(tabs, hidden, 'use')
  // When / Then
  assert.equal(hiddenSectionRedirect(tabs, hidden, visible, '/app/agents/a1/chat'), '/app/agents/a1/files')
  assert.equal(hiddenSectionRedirect(tabs, hidden, visible, '/app/agents/a1/chat/session-1'), '/app/agents/a1/files')
})

test('Given a hidden section with a different route segment, when its route is open, then it redirects', () => {
  // Given
  const hidden = ['people']
  const visible = visibleAgentTabs(tabs, hidden, 'use')
  // When / Then
  assert.equal(hiddenSectionRedirect(tabs, hidden, visible, '/app/agents/a1/relationships'), '/app/agents/a1/chat')
  assert.equal(hiddenSectionRedirect(tabs, hidden, visible, '/app/agents/a1/people'), null)
})

test('Given visible, unknown or sibling-prefixed paths, when checked, then nothing redirects', () => {
  // Given
  const hidden = ['chat']
  const visible = visibleAgentTabs(tabs, hidden, 'use')
  // When / Then
  for (const path of ['/app/agents/a1/files', '/app/agents/a1', '/app/agents/a1/chatter', '/app/agents/new']) {
    assert.equal(hiddenSectionRedirect(tabs, hidden, visible, path), null, path)
  }
})

test('Given tenant data has not arrived, when hidden sections are empty, then no path redirects', () => {
  // Given
  const visible = visibleAgentTabs(tabs, [], 'use')
  // When / Then
  assert.equal(hiddenSectionRedirect(tabs, [], visible, '/app/agents/a1/chat'), null)
})

test('Given no visible tab remains, when the path is hidden, then the member stays put', () => {
  // Given
  const hidden = ['chat', 'files', 'people', 'settings']
  const visible = visibleAgentTabs(tabs, hidden, 'use')
  // When / Then
  assert.deepEqual(visible, [])
  assert.equal(hiddenSectionRedirect(tabs, hidden, visible, '/app/agents/a1/chat'), null)
})

test('Given an agent id that needs encoding, when redirecting, then the target path keeps it encoded', () => {
  // Given
  const hidden = ['chat']
  const visible = visibleAgentTabs(tabs, hidden, 'use')
  // When
  const target = hiddenSectionRedirect(tabs, hidden, visible, `/app/agents/${encodeURIComponent('a/1')}/chat`)
  // Then
  assert.equal(target, `/app/agents/${encodeURIComponent('a/1')}/files`)
})
