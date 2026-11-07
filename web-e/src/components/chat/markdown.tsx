import { memo } from 'react'

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
}

function renderInline(text: string): string {
  return escapeHtml(text)
    .replaceAll(/`([^`]+)`/g, '<code class="rounded bg-background/70 px-1 py-0.5 text-[0.85em]">$1</code>')
    .replaceAll(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replaceAll(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a class="underline" href="$2" target="_blank" rel="noreferrer">$1</a>')
}

const MarkdownBlock = memo(function MarkdownBlock({ block }: { readonly block: string }) {
  const trimmed = block.trim()
  if (trimmed.startsWith('```')) {
    const body = trimmed.replace(/^```[a-zA-Z]*\n?/, '').replace(/```$/, '')
    return <pre className="overflow-x-auto rounded-lg bg-background/80 p-2 text-xs" dangerouslySetInnerHTML={{ __html: `<code>${escapeHtml(body)}</code>` }} />
  }
  if (trimmed.startsWith('# ')) return <h3 className="font-semibold" dangerouslySetInnerHTML={{ __html: renderInline(trimmed.slice(2)) }} />
  if (trimmed.startsWith('## ')) return <h4 className="font-semibold" dangerouslySetInnerHTML={{ __html: renderInline(trimmed.slice(3)) }} />
  if (/^[-*] /.test(trimmed)) {
    const items = trimmed
      .split('\n')
      .filter((line) => /^[-*] /.test(line))
      .map((line) => `<li>${renderInline(line.replace(/^[-*] /, ''))}</li>`)
      .join('')
    return <ul className="list-disc space-y-0.5 pl-5" dangerouslySetInnerHTML={{ __html: items }} />
  }
  return <p dangerouslySetInnerHTML={{ __html: renderInline(block).replaceAll('\n', '<br />') }} />
})

export const ChatMarkdown = memo(function ChatMarkdown({ text }: { readonly text: string }) {
  let start = 0
  return <div className="space-y-2 [&_a]:text-inherit">
    {text.split(/\n{2,}/).map((block) => {
      const key = start
      start += block.length + 1
      return <MarkdownBlock key={key} block={block} />
    })}
  </div>
})
