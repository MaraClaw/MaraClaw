import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement, type ComponentType } from 'react'
import { prerender } from 'react-dom/static'
import { ChunkLoadError, retryingImport, retryingLazy } from '../src/lib/retrying-import.ts'

const failure = new TypeError('Failed to fetch dynamically imported module')

async function render(Route: ComponentType): Promise<string> {
  const { prelude } = await prerender(createElement(Route), { onError: () => undefined })
  return new Response(prelude).text()
}

test('Given a failed import, retryingImport forgets it so the next call imports again, then shares the loaded module', async () => {
  let imports = 0
  const load = retryingImport(async () => {
    imports++
    if (imports === 1) throw failure
    return { page: 'loaded' }
  })
  const first = load()
  assert.equal(load(), first)
  await assert.rejects(first, (error) => error === failure)
  const second = load()
  assert.notEqual(second, first)
  assert.deepEqual(await second, { page: 'loaded' })
  assert.equal(load(), second)
  assert.equal(imports, 2)
})

test('Given a route chunk that fails to download, retryingLazy replays the failure until retry swaps in a fresh import', async () => {
  let imports = 0
  const Page = () => createElement('p', null, 'Activity page')
  const Route = retryingLazy(async () => {
    imports++
    if (imports === 1) throw failure
    return Page
  })
  let caught: unknown
  await assert.rejects(render(Route), (error) => {
    caught = error
    return error instanceof ChunkLoadError && error.cause === failure
  })
  // React.lazy keeps the rejected import: another render throws the same error without downloading again.
  await assert.rejects(render(Route), (error) => error === caught)
  assert.equal(imports, 1)

  assert.ok(caught instanceof ChunkLoadError)
  caught.retry()
  assert.match(await render(Route), /Activity page/)
  assert.equal(imports, 2)

  // A retry left over from the earlier failure never discards the route that has since loaded.
  caught.retry()
  assert.match(await render(Route), /Activity page/)
  assert.equal(imports, 2)
})
