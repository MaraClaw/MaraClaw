import { admissionDetailSchema } from '../src/lib/admissions-api.ts'

export const pendingAdmission = admissionDetailSchema.parse({
  id: '381bf275-fd20-4b0f-a79d-1f2c4519d258',
  status: 'pending',
  source_tenant_id: '64c0b6ca-0312-42aa-996d-ff606043337a',
  source_tenant: { id: '64c0b6ca-0312-42aa-996d-ff606043337a', name: 'Source company' },
  target_tenant: { id: '86b8142b-31e6-44ab-a35e-e10998037030', name: 'Target company' },
  method: 'invitation',
  created_at: '2026-10-20T09:00:00Z',
  decided_at: null,
  decision_reason: null,
  applicant: { id: '9693e39c-ec62-4003-9701-7b5b530c9662', email: 'applicant@example.test' },
  decided_by_user_id: null,
})

export const approvedAdmission = admissionDetailSchema.parse({
  ...pendingAdmission,
  status: 'approved',
  decided_at: '2026-10-20T10:00:00Z',
  decision_reason: 'Confirmed membership',
  decided_by_user_id: '0a8be2b3-75d3-4b22-b071-de135657ef3d',
})
