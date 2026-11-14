export const plans = [
  { id: 'pro', name: 'Pro', currency: 'USD', monthly_price_usd: 79, annual_price_usd: 790, included_accounts: 5, additional_account_monthly_usd: 5, max_agents_per_account: 2, included_agents: 2, additional_agent_monthly_usd: 35, storage_limit_bytes: 2147483648, max_file_size_bytes: 26214400, daily_messages_per_agent: 100, guest_memory_bytes: 2147483648, guest_millicpus: 1500, guest_cpu_shares: 1024, guest_pids_limit: 512, workspace_disk_bytes: 3221225472, storage_addon_bytes: 10737418240, storage_addon_monthly_usd: 5, workspace_disk_addon_bytes: 5368709120, workspace_disk_addon_monthly_usd: 3 },
  { id: 'max', name: 'Max', currency: 'USD', monthly_price_usd: 249, annual_price_usd: 2490, included_accounts: 15, additional_account_monthly_usd: 4, max_agents_per_account: 3, included_agents: 5, additional_agent_monthly_usd: 49, storage_limit_bytes: 5368709120, max_file_size_bytes: 52428800, daily_messages_per_agent: 200, guest_memory_bytes: 3221225472, guest_millicpus: 2000, guest_cpu_shares: 1536, guest_pids_limit: 768, workspace_disk_bytes: 5368709120, storage_addon_bytes: 10737418240, storage_addon_monthly_usd: 5, workspace_disk_addon_bytes: 5368709120, workspace_disk_addon_monthly_usd: 3 },
  { id: 'ultra', name: 'Ultra', currency: 'USD', monthly_price_usd: 699, annual_price_usd: 6990, included_accounts: 40, additional_account_monthly_usd: 3, max_agents_per_account: 5, included_agents: 10, additional_agent_monthly_usd: 79, storage_limit_bytes: 10737418240, max_file_size_bytes: 104857600, daily_messages_per_agent: 500, guest_memory_bytes: 5368709120, guest_millicpus: 3000, guest_cpu_shares: 2048, guest_pids_limit: 1024, workspace_disk_bytes: 8589934592, storage_addon_bytes: 10737418240, storage_addon_monthly_usd: 5, workspace_disk_addon_bytes: 5368709120, workspace_disk_addon_monthly_usd: 3 },
]

export const quote = {
  organization_name: 'Example Organization',
  email: 'owner@example.test',
  phone: '+84912345678',
  tier: 'ultra',
  billing: 'annual',
  plan: plans[2],
  amount_usd: 6990,
  usd_vnd_rate: 25000,
  amount_vnd: 174750000,
  recipient: { bank_name: 'Example Bank', account_number: '0123456789', branch_name: 'Example Branch' },
  reference: 'MO7K2QX4ZA',
  expires_at: '2026-11-21T10:00:00Z',
  quote_token: 'payload.signature',
}
