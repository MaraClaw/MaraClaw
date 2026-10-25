import assert from 'node:assert/strict'
import { test } from 'node:test'
import { z } from 'zod'
import { completeOnboarding, getOnboardingStatus, startOnboarding } from '../src/lib/onboarding-api.ts'

const status = { exists: false, status: 'not_started', current_step: 'entry', entry_mode: null,
  personal_assistant_agent_id: null, completed_at: null }

for (const request of [getOnboardingStatus, startOnboarding, completeOnboarding]) {
  test(`${request.name} accepts required nullable fields from HTTP`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(status))
    // When
    const result = await request()
    // Then
    assert.deepEqual(result, status)
  })
  for (const field of ['entry_mode', 'personal_assistant_agent_id', 'completed_at']) {
    test(`${request.name} rejects a missing required ${field} at the HTTP boundary`, async (t) => {
      // Given
      const payload = Object.fromEntries(Object.entries(status).filter(([key]) => key !== field))
      t.mock.method(globalThis, 'fetch', async () => Response.json(payload))
      // When / Then
      await assert.rejects(request(), z.ZodError)
    })
  }
}
