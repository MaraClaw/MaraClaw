import type { ChatSubmission } from './protocol.ts'

export function createAttachmentSubmission(uploaded: {
  readonly workspace_path: string
  readonly filename: string
  readonly extracted_text: string
}, prose?: Pick<ChatSubmission, 'content' | 'display_content'>): ChatSubmission {
  const attached = uploaded.extracted_text || `[Uploaded ${uploaded.filename}]`
  return Object.freeze({
    client_submission_id: crypto.randomUUID(),
    content: prose ? `${prose.content}\n\n${attached}` : attached,
    display_content: prose ? prose.display_content ?? prose.content : uploaded.filename,
    file_name: uploaded.filename,
    attachments: Object.freeze([Object.freeze({ path: uploaded.workspace_path, name: uploaded.filename })]),
  })
}
