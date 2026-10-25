import { z } from 'zod'

import { authSession, type SessionOwner } from './auth-session.ts'
import { apiRequest } from './http.ts'

const messageSchema = z.object({
  entry_id: z.string().min(1),
  message_id: z.string().min(1).optional(),
  client_submission_id: z.string().min(1).nullable().optional(),
  role: z.string().min(1),
  content: z.string(),
  created_at: z.string().nullable().optional(),
  file_name: z.string().optional(),
  attachments: z.array(z.object({ path: z.string().min(1), name: z.string().min(1) }).readonly()).readonly().optional(),
  toolName: z.string().optional(),
  toolArgs: z.json().optional(),
  toolStatus: z.string().optional(),
  toolResult: z.string().optional(),
  toolThinking: z.string().optional(),
  thinking: z.string().optional(),
  sender_name: z.string().optional(),
  participant_id: z.string().optional(),
}).transform((row) => ({
  entry_id: row.entry_id, role: row.role, content: row.content,
  ...(row.message_id === undefined ? {} : { message_id: row.message_id }),
  ...(row.client_submission_id === undefined ? {} : { client_submission_id: row.client_submission_id }),
  ...(row.created_at === undefined ? {} : { created_at: row.created_at }),
  ...(row.file_name === undefined ? {} : { file_name: row.file_name }),
  ...(row.attachments === undefined ? {} : { attachments: row.attachments }),
  ...(row.toolName === undefined ? {} : { toolName: row.toolName }),
  ...(row.toolArgs === undefined ? {} : { toolArgs: row.toolArgs }),
  ...(row.toolStatus === undefined ? {} : { toolStatus: row.toolStatus }),
  ...(row.toolResult === undefined ? {} : { toolResult: row.toolResult }),
  ...(row.toolThinking === undefined ? {} : { toolThinking: row.toolThinking }),
  ...(row.thinking === undefined ? {} : { thinking: row.thinking }),
  ...(row.sender_name === undefined ? {} : { sender_name: row.sender_name }),
  ...(row.participant_id === undefined ? {} : { participant_id: row.participant_id }),
})).readonly()
const pageSchema = z.object({
  items: z.array(messageSchema).readonly(),
  next_cursor: z.string().min(1).nullable(),
  has_more: z.boolean(),
}).refine((page) => page.has_more === (page.next_cursor !== null), { message: 'Inconsistent history cursor' }).readonly()

export type ChatHistoryPage = z.infer<typeof pageSchema>
export type ChatHistoryTarget = { readonly agentId: string; readonly sessionId: string }
type PageRequest = {
  readonly cursor?: string | null
  readonly owner?: SessionOwner
  readonly signal?: AbortSignal
}

export async function listChatHistoryPage(target: ChatHistoryTarget, request: PageRequest = {}): Promise<ChatHistoryPage> {
  const params = new URLSearchParams({ limit: '50' })
  if (request.cursor != null) params.set('cursor', request.cursor)
  const owner = request.owner ?? authSession.capture()
  return apiRequest(
    `/api/agents/${encodeURIComponent(target.agentId)}/sessions/${encodeURIComponent(target.sessionId)}/messages/page?${params}`,
    { schema: pageSchema, owner, ...(request.signal === undefined ? {} : { signal: request.signal }) },
  )
}
