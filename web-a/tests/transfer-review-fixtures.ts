import { transferSummarySchema } from '../src/lib/transfer-reviews-api.ts'

export const pendingTransfer = transferSummarySchema.parse({
  id: '4ea38060-6d93-4b61-a74e-4fda5d53c17f',
  applicant_user_id: '2f2d06db-0a93-41aa-9217-58cb40ac2510',
  source_tenant_id: '7bb549dc-0b04-4b0c-85cf-8109cc0891ae',
  target_tenant_id: 'a73c5c64-45d6-4689-afd1-467b208c8962',
  choice: 'paid', status: 'payment_review', fee_amount_minor: 12500, currency: 'CNY',
  instructions: 'Synthetic payment instructions.\nVerify the request reference before approval.',
  created_at: '2026-10-23T10:00:00Z', free_consent_at: null, payment_confirmed_at: null,
  reviewed_at: null, reviewed_by_user_id: null, review_reason: null, admission_request_id: null,
})

export const admittedTransfer = transferSummarySchema.parse({
  ...pendingTransfer, status: 'admission_created', payment_confirmed_at: '2026-10-24T10:00:00Z',
  reviewed_at: '2026-10-24T10:00:00Z', reviewed_by_user_id: '7d0fd54e-6b7c-45f1-b89e-4f3de263966f',
  admission_request_id: '37c704a3-b0d7-4194-a4a0-a0494be08fe0',
})
