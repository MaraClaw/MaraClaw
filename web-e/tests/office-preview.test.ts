import assert from 'node:assert/strict'
import { test } from 'node:test'
import { loadOfficeViewer, officeFormat, OfficePreviewTimeoutError } from '../src/lib/office-preview.ts'

test('Office parser transfer leaves cached input bytes intact', async () => {
  // Given
  const bytes = new Uint8Array([80, 75, 3, 4])
  const lifetime = new AbortController()
  let destroyed = 0
  const viewer = {
    load: async (source: ArrayBuffer) => { structuredClone(source, { transfer: [source] }); assert.equal(source.byteLength, 0) },
    destroy: () => { destroyed++ },
  }
  // When
  await loadOfficeViewer(viewer, bytes, lifetime.signal)
  lifetime.abort()
  // Then
  assert.deepEqual([...bytes], [80, 75, 3, 4])
  assert.equal(destroyed, 1)
})

test('closing a loading Office view destroys it and rejects a late completion', async () => {
  // Given
  const pending = Promise.withResolvers<void>()
  const lifetime = new AbortController()
  let destroyed = 0
  const result = loadOfficeViewer({ load: () => pending.promise, destroy: () => { destroyed++ } }, new Uint8Array([1]), lifetime.signal)
  const rejected = assert.rejects(result, { name: 'AbortError' })
  // When
  lifetime.abort()
  pending.resolve()
  // Then
  await rejected
  assert.equal(destroyed, 1)
})

test('Office parse failure destroys the owned viewer', async () => {
  // Given
  const failure = new Error('corrupt fixture')
  let destroyed = 0
  const viewer = { load: () => Promise.reject(failure), destroy: () => { destroyed++ } }
  // When / Then
  await assert.rejects(loadOfficeViewer(viewer, new Uint8Array([1]), new AbortController().signal), failure)
  assert.equal(destroyed, 1)
})

test('an unresponsive Office load is destroyed at the overall deadline', async (context) => {
  // Given
  context.mock.timers.enable({ apis: ['setTimeout'] })
  let destroyed = 0
  const result = loadOfficeViewer({ load: () => new Promise(() => {}), destroy: () => { destroyed++ } }, new Uint8Array([1]), new AbortController().signal)
  const rejected = assert.rejects(result, OfficePreviewTimeoutError)
  // When
  context.mock.timers.tick(20_000)
  // Then
  await rejected
  assert.equal(destroyed, 1)
})

test('only modern OOXML extensions select the local Office parser', () => {
  // Given
  const names = ['report.DOCX', 'book.xlsx', 'slides.pptx', 'legacy.doc', 'legacy.xls', 'legacy.ppt', 'document.docm']
  // When
  const formats = names.map(officeFormat)
  // Then
  assert.deepEqual(formats, ['docx', 'xlsx', 'pptx', undefined, undefined, undefined, undefined])
})
