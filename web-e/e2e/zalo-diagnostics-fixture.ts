import { disconnected, generation } from './linked-channels-fixture'

export const diagnostics = {
  reconnect_attempts: 3,
  retry_at: '2030-01-01T00:00:30+00:00',
  last_error: 'transport_failed',
  last_error_at: '2029-12-31T23:59:30Z',
  connected_at: '2029-12-31T23:50:00Z',
  last_activity_at: '2029-12-31T23:59:00Z',
  heartbeat_at: '2029-12-31T23:59:15Z',
  heartbeat_failures: 2,
} as const

export const diagnosticCases = {
  backoff: { ...disconnected, generation, attempt_id: generation, revision: 4, state: 'reconnecting', resumable: true, diagnostics },
  connected: { ...disconnected, generation, attempt_id: generation, revision: 5, state: 'connected', resumable: true, diagnostics: { ...diagnostics, reconnect_attempts: 0, retry_at: null, heartbeat_failures: 0, last_error: null, last_error_at: null } },
  exhausted: { ...disconnected, generation, attempt_id: generation, revision: 6, state: 'paused', resumable: true, diagnostics: { ...diagnostics, reconnect_attempts: 12, retry_at: null } },
  replaced: { ...disconnected, generation, attempt_id: generation, revision: 7, state: 'failed', resumable: true, diagnostics: { ...diagnostics, retry_at: null, last_error: 'session_replaced' } },
} as const
