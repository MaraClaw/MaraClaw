import { z } from 'zod'
import { apiRequest } from './http.ts'

const onboardingStatusSchema = z.looseObject({
  exists: z.boolean(),
  status: z.string(),
  current_step: z.string(),
  entry_mode: z.string().nullable(),
  personal_assistant_agent_id: z.string().nullable(),
  completed_at: z.string().nullable(),
})

export type OnboardingStatus = z.infer<typeof onboardingStatusSchema>

export async function getOnboardingStatus(): Promise<OnboardingStatus> {
  return apiRequest('/api/onboarding/status', { schema: onboardingStatusSchema })
}

export async function startOnboarding(entryMode = 'join'): Promise<OnboardingStatus> {
  return apiRequest('/api/onboarding/start', { schema: onboardingStatusSchema, method: 'POST', body: { entry_mode: entryMode } })
}

export async function completeOnboarding(): Promise<OnboardingStatus> {
  return apiRequest('/api/onboarding/complete', { schema: onboardingStatusSchema, method: 'POST' })
}

export const ONBOARDING_SKIP_KEY = 'maraclaw-onboarding-skipped'

export function markOnboardingSkipped(): void {
  try {
    localStorage.setItem(ONBOARDING_SKIP_KEY, '1')
  } catch {
    // ignore
  }
}

export function wasOnboardingSkipped(): boolean {
  try {
    return localStorage.getItem(ONBOARDING_SKIP_KEY) === '1'
  } catch {
    return false
  }
}
