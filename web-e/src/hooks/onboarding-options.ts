import type { UseMutationOptions } from '@tanstack/react-query'
import { completeOnboarding, createPersonalAssistant, startOnboarding } from '../lib/onboarding-api.ts'
import type { AssistantStartFailure, PersonalAssistantInput, PersonalAssistantResponse } from '../lib/onboarding-api.ts'
import type { SessionQueryScope } from './session-query-scope.ts'

type OnboardingEffects = {
  readonly ready: (agent: PersonalAssistantResponse['agent']) => void
  readonly startFailed: (failure: AssistantStartFailure) => void
  readonly error: (error: Error) => void
}

export function assistantOnboardingOptions(
  scope: SessionQueryScope,
  effects: OnboardingEffects,
): UseMutationOptions<PersonalAssistantResponse, Error, PersonalAssistantInput> {
  return {
    mutationFn: async (values) => {
      await startOnboarding('join')
      scope.assertCurrent()
      const created = await createPersonalAssistant(values)
      scope.assertCurrent()
      switch (created.start.status) {
        case 'started': {
          const onboarding = await completeOnboarding()
          scope.assertCurrent()
          return { ...created, onboarding }
        }
        case 'failed':
          return created
        default: {
          const unexpected: never = created.start
          throw new TypeError('Unexpected assistant start outcome', { cause: unexpected })
        }
      }
    },
    async onSuccess(result) {
      switch (result.start.status) {
        case 'started':
          await Promise.all([
            scope.client.cancelQueries({ queryKey: ['onboarding'], exact: true }),
            scope.client.cancelQueries({ queryKey: ['agents'], exact: true }),
          ])
          scope.assertCurrent()
          scope.client.setQueryData(['onboarding'], result.onboarding)
          await scope.client.invalidateQueries({ queryKey: ['agents'], exact: true, refetchType: 'all' })
          scope.assertCurrent()
          effects.ready(result.agent)
          return
        case 'failed':
          effects.startFailed(result.start)
          return
        default: {
          const unexpected: never = result.start
          throw new TypeError('Unexpected assistant start outcome', { cause: unexpected })
        }
      }
    },
    onError: effects.error,
  }
}
