export type ChatAttachment = { readonly path: string; readonly name: string }
export type ChatJsonValue = string | number | boolean | null | readonly ChatJsonValue[] | { readonly [key: string]: ChatJsonValue }

export type ChatMessage = {
  readonly client_submission_id?: string | null
  readonly message_id?: string
  readonly entry_id?: string
  readonly role: string
  readonly content: string
  readonly created_at?: string | null
  readonly file_name?: string
  readonly attachments?: readonly ChatAttachment[]
  readonly toolName?: string
  readonly toolArgs?: ChatJsonValue
  readonly toolStatus?: string
  readonly toolResult?: string
  readonly toolThinking?: string
  readonly thinking?: string
  readonly sender_name?: string
  readonly participant_id?: string
}

export function getChatMessageText(message: ChatMessage): string {
  if (message.role !== 'user') return message.content
  const attached = message.attachments?.find((attachment) => message.content.startsWith(`[file:${attachment.name}]\n`))
  const content = attached ? message.content.slice(`[file:${attached.name}]\n`.length) : message.content
  return message.attachments?.some((attachment) => attachment.name === content) ? '' : content
}
