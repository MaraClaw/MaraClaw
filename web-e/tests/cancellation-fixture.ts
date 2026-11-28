import assert from 'node:assert/strict'

/**
 * Proves every signal a request handed to `fetch` follows the caller's: still live once the request settled, then
 * aborted with the caller's own reason. The HTTP client joins its deadline to the caller's signal, so the contract is
 * propagation, not identity.
 */
export function assertForwardsCancellation(forwarded: readonly (AbortSignal | null | undefined)[], caller: AbortController): void {
  assert.notEqual(forwarded.length, 0)
  for (const signal of forwarded) assert.equal(signal?.aborted, false)
  const reason = new Error('caller cancelled')
  caller.abort(reason)
  for (const signal of forwarded) assert.equal(signal?.reason, reason)
}
