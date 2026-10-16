export type ChatMessage = {
  readonly message_id?: string
  readonly entry_id?: string
  readonly role: string
  readonly content: string
  readonly created_at?: string | null
  readonly file_name?: string
  readonly toolName?: string
  readonly toolStatus?: string
  readonly toolResult?: string
  readonly thinking?: string
}
