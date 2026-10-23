import { getChatMessageText, type ChatMessage } from '@/lib/chat/message'
import { AttachmentCard } from './attachment-card'
import { ChatMarkdown } from './markdown'

export function ChatMessageContent({ agentId, message }: { readonly agentId: string; readonly message: ChatMessage }) {
  const content = getChatMessageText(message)
  return <>
    {message.role === 'assistant' ? <ChatMarkdown text={content} /> : content}
    {message.attachments?.length ? <div className="space-y-2">
      {message.attachments.map((attachment) => <AttachmentCard key={attachment.path} agentId={agentId} attachment={attachment} />)}
    </div> : null}
  </>
}
