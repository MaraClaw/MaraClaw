import assert from 'node:assert/strict'
import test from 'node:test'

import { admissionDetailSchema } from '../src/lib/admissions-api.ts'

test('admission details accept an applicant without an account username', () => {
  const input = {
    id: '00000000-0000-4000-8000-000000000001', status: 'pending',
    source_tenant_id: null,
    target_tenant: { id: '00000000-0000-4000-8000-000000000002', name: 'Target' },
    method: 'invitation', created_at: '2026-10-20T00:00:00Z',
    decided_at: null, decision_reason: null,
    applicant: { id: '00000000-0000-4000-8000-000000000003', email: 'applicant@example.test' },
    source_tenant: null, decided_by_user_id: null,
  }

  const detail = admissionDetailSchema.parse(input)

  assert.deepEqual(detail.applicant, input.applicant)
})
