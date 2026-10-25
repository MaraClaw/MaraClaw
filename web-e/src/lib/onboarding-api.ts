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

const personalAssistantResponseSchema = z.looseObject({
  agent: z.looseObject({ id: z.string(), name: z.string() }),
  onboarding: onboardingStatusSchema,
  api_key: z.unknown().optional(),
  start: z.discriminatedUnion('status', [
    z.object({ status: z.literal('started') }),
    z.object({ status: z.literal('failed'), code: z.string(), message: z.string() }),
  ]),
})

export type OnboardingStatus = z.infer<typeof onboardingStatusSchema>
export type PersonalAssistantResponse = z.infer<typeof personalAssistantResponseSchema>
export type AssistantStartFailure = Extract<PersonalAssistantResponse['start'], { status: 'failed' }>
export type PersonalAssistantInput = {
  readonly name: string
  readonly personality?: string
  readonly work_style?: string
  readonly boundaries?: string
}

export async function getOnboardingStatus(): Promise<OnboardingStatus> {
  return apiRequest('/api/onboarding/status', { schema: onboardingStatusSchema })
}

export async function startOnboarding(entryMode = 'join'): Promise<OnboardingStatus> {
  return apiRequest('/api/onboarding/start', { schema: onboardingStatusSchema, method: 'POST', body: { entry_mode: entryMode } })
}

export async function createPersonalAssistant(body: PersonalAssistantInput): Promise<PersonalAssistantResponse> {
  return apiRequest('/api/onboarding/personal-assistant', { schema: personalAssistantResponseSchema, method: 'POST', body })
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
