import type { ChatSubmission } from './protocol.ts'

export function createAttachmentSubmission(uploaded: {
  readonly workspace_path: string
  readonly filename: string
  readonly extracted_text: string
}): ChatSubmission {
  return Object.freeze({
    client_submission_id: crypto.randomUUID(),
    content: uploaded.extracted_text || `[Uploaded ${uploaded.filename}]`,
    display_content: uploaded.filename,
    file_name: uploaded.filename,
    attachments: Object.freeze([Object.freeze({ path: uploaded.workspace_path, name: uploaded.filename })]),
  })
}
