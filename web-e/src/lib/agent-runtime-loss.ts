import type { AgentRuntimeLossOut } from './engine-contracts.generated.ts'
import { startAgent, startNewContainer } from './agents-api.ts'
import { ApiError } from './http.ts'
import type { TranslationKey } from './i18n/catalog.ts'

export type RuntimeLossReason = AgentRuntimeLossOut['reason']

export const runtimeLossSummary = {
  container_stopped: 'The container this agent runs in has stopped, so it can’t reply right now.',
  container_missing: 'The container this agent runs in is no longer available, so it can’t reply right now.',
  image_unavailable: 'The container this agent runs in is no longer available, and the platform can’t create a new one yet. The platform team has been notified.',
} as const satisfies Record<RuntimeLossReason, TranslationKey>

export function canManageAgent(
  agent: { readonly access_level?: string | null | undefined; readonly creator_id: string },
  userId: string | undefined,
): boolean {
  return agent.access_level === 'manage' || userId === agent.creator_id
}

export type StartNewContainerFailure =
  | 'forbidden' | 'setup_incomplete' | 'container_present' | 'image_unavailable' | 'start_ineligible'
  | 'start_cancelled' | 'capacity_exceeded' | 'runtime_attention' | 'start_failed' | 'update_required' | 'update_in_progress'
  | 'unknown'

export const startNewContainerFailureMessage = {
  forbidden: 'Only this agent’s managers can start a new container.',
  setup_incomplete: 'Finish this agent’s setup before starting a new container.',
  container_present: 'This agent already has a container. This page will update shortly.',
  image_unavailable: 'No runtime image is available right now. Contact your company admin.',
  start_ineligible: 'This agent can’t be started right now. Check its lifecycle status in Settings or contact your company admin.',
  start_cancelled: 'The new container was stopped before it finished starting.',
  capacity_exceeded: 'There isn’t enough capacity to start a new container right now. Try again in a few minutes.',
  runtime_attention: 'This agent’s runtime needs attention before it can start. Contact your company admin.',
  start_failed: 'The new container couldn’t be started. Try again in a moment.',
  update_required: 'This agent must be upgraded before a new container can start. Contact your company admin.',
  update_in_progress: 'This agent is being upgraded. Start a new container once the upgrade finishes.',
  unknown: 'Unable to start a new container. Try again, or contact your company admin if it keeps happening.',
} as const satisfies Record<StartNewContainerFailure, TranslationKey>

const SETUP_INCOMPLETE_DETAIL = 'Agent setup is incomplete'

const failureByStatusAndCode = new Map<string, StartNewContainerFailure>([
  ['409:guest_setup_incomplete', 'setup_incomplete'],
  ['409:guest_container_present', 'container_present'],
  ['409:guest_image_unavailable', 'image_unavailable'],
  ['409:guest_start_ineligible', 'start_ineligible'],
  ['409:guest_start_cancelled', 'start_cancelled'],
  ['409:guest_memory_capacity_exceeded', 'capacity_exceeded'],
  ['409:guest_memory_unbounded', 'runtime_attention'],
  ['409:guest_recreation_required', 'runtime_attention'],
  ['502:guest_start_failed', 'start_failed'],
  ['423:openclaw_update_required', 'update_required'],
  ['409:openclaw_update_in_progress', 'update_in_progress'],
])

function detailCode(detail: unknown): string | null {
  return detail !== null && typeof detail === 'object' && 'code' in detail && typeof detail.code === 'string'
    ? detail.code : null
}

export function classifyStartNewContainerError(error: unknown): StartNewContainerFailure {
  if (!(error instanceof ApiError)) return 'unknown'
  if (error.status === 403) return 'forbidden'
  if (error.status === 409 && error.detail === SETUP_INCOMPLETE_DETAIL) return 'setup_incomplete'
  const code = detailCode(error.detail)
  return code === null ? 'unknown' : failureByStatusAndCode.get(`${error.status}:${code}`) ?? 'unknown'
}

export function startNewContainerErrorMessage(error: unknown): (typeof startNewContainerFailureMessage)[StartNewContainerFailure] {
  return startNewContainerFailureMessage[classifyStartNewContainerError(error)]
}

const startStoppedFailureMessage = {
  ...startNewContainerFailureMessage,
  forbidden: 'Only this agent’s managers can start it.',
  setup_incomplete: 'Finish this agent’s setup before starting it.',
  start_cancelled: 'This agent was stopped before it finished starting.',
  capacity_exceeded: 'There isn’t enough capacity to start this agent right now. Try again in a few minutes.',
  start_failed: 'This agent couldn’t be started. Try again in a moment.',
  update_required: 'This agent must be upgraded before it can start. Contact your company admin.',
  update_in_progress: 'This agent is being upgraded. Start it again once the upgrade finishes.',
  unknown: 'Unable to start this agent. Try again, or contact your company admin if it keeps happening.',
} as const satisfies Record<StartNewContainerFailure, TranslationKey>

const newContainerRecovery = {
  start: startNewContainer,
  label: 'Start a new container',
  pending: 'Starting a new container...',
  success: 'New container started',
  managerMessage: 'Your conversations, memory, skills, integrations and schedules are kept. Any message you already sent will be answered once a new container starts.',
  memberMessage: 'Ask the agent’s manager to start a new container. Your messages are saved.',
  errorMessage: startNewContainerErrorMessage,
} as const

export const runtimeLossRecovery = {
  container_missing: newContainerRecovery,
  image_unavailable: newContainerRecovery,
  container_stopped: {
    start: startAgent,
    label: 'Start',
    pending: 'Starting...',
    success: 'Agent started',
    managerMessage: 'Your conversations, memory, skills, integrations and schedules are kept. Any message you already sent will be answered once this agent starts again.',
    memberMessage: 'Ask the agent’s manager to start it again. Your messages are saved.',
    errorMessage: (error: unknown) => startStoppedFailureMessage[classifyStartNewContainerError(error)],
  },
} as const satisfies Record<RuntimeLossReason, {
  readonly start: typeof startAgent
  readonly label: TranslationKey
  readonly pending: TranslationKey
  readonly success: TranslationKey
  readonly managerMessage: TranslationKey
  readonly memberMessage: TranslationKey
  readonly errorMessage: (error: unknown) => TranslationKey
}>
