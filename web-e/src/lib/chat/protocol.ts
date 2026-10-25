import { ChatEventType, ChatServerEventSchema } from '../engine-contracts.generated.ts'
import type { ChatServerEvent, CorrelatedTurnRejectedEvent, InvalidSubmissionRejectedEvent, OnboardingTurnRejectedEvent, TurnAcceptedEvent } from '../engine-contracts.generated.ts'
import type { ChatAttachment } from './message.ts'

export { ChatEventType } from '../engine-contracts.generated.ts'

export type ChatSubmission = {
  readonly client_submission_id: string
  readonly content: string
  readonly display_content?: string
  readonly file_name?: string
  readonly attachments?: readonly ChatAttachment[]
  readonly model_id?: string
  readonly kind?: never
}

export type ChatOutbound = ChatSubmission | { readonly kind: 'onboarding_trigger' }
export const inboundSchema = ChatServerEventSchema
export type ChatInbound = ChatServerEvent
export type ChatTurnAcceptance = TurnAcceptedEvent
export type ChatTurnRejection = CorrelatedTurnRejectedEvent | OnboardingTurnRejectedEvent | InvalidSubmissionRejectedEvent

export function archivedSessionId(event: ChatInbound): string | undefined {
  return event.type === ChatEventType.ERROR && typeof event.content === 'object' ? event.content.session_id : undefined
}

export function isTurnAccepted(event: ChatInbound): event is ChatTurnAcceptance {
  return event.type === ChatEventType.TURN_ACCEPTED
}

export function isTurnRejected(event: ChatInbound): event is ChatTurnRejection {
  return event.type === ChatEventType.TURN_REJECTED
}
