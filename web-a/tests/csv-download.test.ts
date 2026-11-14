import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'

import { downloadCsvFile } from '../src/lib/csv-download.ts'

function ownDocument(context: TestContext, failClick = false) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'document')
  const events: string[] = []
  const anchor = {
    href: '', download: '',
    click: () => { events.push('click'); if (failClick) throw new Error('Download blocked') },
    remove: () => events.push('remove'),
  }
  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: { createElement: () => anchor, body: { append: () => events.push('append') } },
  })
  context.after(() => {
    if (previous) Object.defineProperty(globalThis, 'document', previous)
    else Reflect.deleteProperty(globalThis, 'document')
  })
  return { anchor, events }
}

test('CSV text is saved as a named UTF-8 file and the object URL outlives the click', async (context) => {
  // Given
  context.mock.timers.enable({ apis: ['setTimeout'] })
  const { anchor, events } = ownDocument(context)
  let saved: Blob | undefined
  context.mock.method(URL, 'createObjectURL', (blob: Blob) => { saved = blob; return 'blob:csv-test' })
  const revoke = context.mock.method(URL, 'revokeObjectURL', (_url: string) => undefined)
  const csv = 'id,name\r\n1,Zoë\r\n'
  // When
  downloadCsvFile(csv, 'resource-pressure-episodes.csv')
  // Then
  assert.equal(anchor.href, 'blob:csv-test')
  assert.equal(anchor.download, 'resource-pressure-episodes.csv')
  assert.deepEqual(events, ['append', 'click', 'remove'])
  assert.equal(saved?.type, 'text/csv;charset=utf-8')
  assert.equal(await saved?.text(), csv)
  assert.equal(revoke.mock.callCount(), 0)
  context.mock.timers.tick(10_000)
  assert.deepEqual(revoke.mock.calls.map((call) => call.arguments[0]), ['blob:csv-test'])
})

test('a blocked click still removes the anchor and releases the object URL', (context) => {
  // Given
  context.mock.timers.enable({ apis: ['setTimeout'] })
  const { events } = ownDocument(context, true)
  context.mock.method(URL, 'createObjectURL', () => 'blob:csv-test')
  const revoke = context.mock.method(URL, 'revokeObjectURL', (_url: string) => undefined)
  // When / Then
  assert.throws(() => downloadCsvFile('id\r\n', 'x.csv'), /Download blocked/)
  assert.deepEqual(events, ['append', 'click', 'remove'])
  context.mock.timers.tick(10_000)
  assert.equal(revoke.mock.callCount(), 1)
})
