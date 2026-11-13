import type { UseMutationOptions } from '@tanstack/react-query'
import { createAgent } from '../lib/agents-api.ts'
import type { AgentOut } from '../lib/agents-api.ts'
import type { AgentCreate } from '../lib/engine-contracts.generated.ts'
import { completeOnboarding, startOnboarding } from '../lib/onboarding-api.ts'
import type { OnboardingStatus } from '../lib/onboarding-api.ts'
import type { SessionQueryScope } from './session-query-scope.ts'

type AssistantOnboardingResult = {
  readonly agent: AgentOut
  readonly onboarding: OnboardingStatus
}

type OnboardingEffects = {
  readonly ready: (agent: AgentOut) => void
  readonly error: (error: Error) => void
}

export function assistantOnboardingOptions(
  scope: SessionQueryScope,
  effects: OnboardingEffects,
): UseMutationOptions<AssistantOnboardingResult, Error, AgentCreate> {
  return {
    mutationFn: async (body) => {
      await startOnboarding('join')
      scope.assertCurrent()
      const agent = await createAgent(body)
      scope.assertCurrent()
      const onboarding = await completeOnboarding()
      scope.assertCurrent()
      return { agent, onboarding }
    },
    async onSuccess({ agent, onboarding }) {
      await Promise.all([
        scope.client.cancelQueries({ queryKey: ['onboarding'], exact: true }),
        scope.client.cancelQueries({ queryKey: ['agents'] }),
      ])
      scope.assertCurrent()
      scope.client.setQueryData(['onboarding'], onboarding)
      await scope.client.invalidateQueries({ queryKey: ['agents'], refetchType: 'all' })
      scope.assertCurrent()
      effects.ready(agent)
    },
    onError: effects.error,
  }
}
