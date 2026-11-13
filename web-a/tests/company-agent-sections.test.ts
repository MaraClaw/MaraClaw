import assert from 'node:assert/strict'
import test from 'node:test'
import { QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import { companyAgentSectionsOptions } from '../src/hooks/company-options.ts'
import { fetchCompanyAgentSections, updateCompanyAgentSections } from '../src/lib/companies-api.ts'
import { ApiError } from '../src/lib/http.ts'

const body = { sections: ['chat', 'files', 'skills'], hidden_sections: ['files'] }

test('agent sections read uses an encoded GET, forwards cancellation and keeps the server order', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _init?: RequestInit) => Response.json(body))
  const controller = new AbortController()
  // When
  const result = await fetchCompanyAgentSections('company/a +?', controller.signal)
  // Then
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/admin/companies/company%2Fa%20%2B%3F/agent-sections')
  assert.equal(init?.method, 'GET')
  assert.strictEqual(init?.signal, controller.signal)
  assert.deepEqual(result, body)
})

test('agent sections update PUTs only the hidden list and returns the stored state', async (t) => {
  // Given
  const stored = { sections: body.sections, hidden_sections: ['chat', 'files'] }
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _init?: RequestInit) => Response.json(stored))
  // When
  const result = await updateCompanyAgentSections('company-a', ['chat', 'files'])
  // Then
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/admin/companies/company-a/agent-sections')
  assert.equal(init?.method, 'PUT')
  assert.deepEqual(JSON.parse(String(init?.body)), { hidden_sections: ['chat', 'files'] })
  assert.deepEqual(result, stored)
})

for (const malformed of [null, {}, { sections: ['chat'] }, { sections: ['chat'], hidden_sections: [1] }]) {
  test(`agent sections reject malformed responses ${JSON.stringify(malformed)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    // When / Then
    await assert.rejects(fetchCompanyAgentSections('company-a'), z.ZodError)
  })
}

for (const status of [404, 422]) {
  test(`agent sections update preserves API rejection ${status}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Rejected' }, { status }))
    // When / Then
    await assert.rejects(updateCompanyAgentSections('company-a', []), (error) => error instanceof ApiError && error.status === status)
  })
}

test('agent sections are cached per company under the shared company prefix', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(companyAgentSectionsOptions('alpha').queryKey, body)
  // When
  await client.invalidateQueries({ queryKey: ['admin-companies'] })
  // Then
  assert.equal(client.getQueryData(companyAgentSectionsOptions('beta').queryKey), undefined)
  assert.equal(client.getQueryState(companyAgentSectionsOptions('alpha').queryKey)?.isInvalidated, true)
})
