import { organizationAdmissionSchema } from '../src/lib/organization-admissions-api.ts'

export const organizationAdmission = organizationAdmissionSchema.parse({
  id: '9ec5429b-f1e1-4673-b541-44652ff1bbd0', organization_name: 'Example Organization', email: 'founder@example.test', tier: 'max',
  plan: { id: 'max', name: 'Max', currency: 'USD', monthly_price_usd: 199,
    included_accounts: 5, additional_account_monthly_usd: 5, max_accounts: 75, max_agents_per_account: 5,
    storage_limit_bytes: 2147483648, max_file_size_bytes: 26214400, daily_messages_per_agent: 200,
    guest_memory_bytes: 3221225472, guest_millicpus: 2000, guest_cpu_shares: 1536, guest_pids_limit: 768,
    workspace_disk_bytes: 5368709120 },
  status: 'pending', created_at: '2026-11-05T00:00:00Z', reviewed_at: null, reviewed_by: null, review_note: null,
  credential_email_status: null, credential_email_error: null,
})

export const approvedOrganizationAdmission = organizationAdmissionSchema.parse({
  ...organizationAdmission, status: 'approved', credential_email_status: 'pending', reviewed_at: '2026-11-05T01:00:00Z',
  reviewed_by: '6e4c635a-65d1-4cf2-87c0-cfc01f6b4921', review_note: 'Application reviewed.',
})
