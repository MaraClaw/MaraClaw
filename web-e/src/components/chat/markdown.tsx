import { memo } from 'react'
import { useTranslation } from '@/hooks/use-translation'
import { parseMarkdownTable, splitMarkdownBlocks } from './markdown-blocks'

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
  const { t: translate } = useTranslation()
  const trimmed = block.trim()
  if (trimmed.startsWith('```')) {
    const body = block.replace(/^\s*`{3,}[^\n]*\n?/, '').replace(/`{3,}\s*$/, '')
    return <pre className="overflow-x-auto rounded-lg bg-background/80 p-2 text-xs" dangerouslySetInnerHTML={{ __html: `<code>${escapeHtml(body)}</code>` }} />
  }
  const table = parseMarkdownTable(block)
  if (table) return <div className="max-w-full overflow-x-auto rounded-lg border border-border [scrollbar-color:var(--muted-foreground)_transparent]" role="region" aria-label={translate('Response table')} tabIndex={0}>
    <table className="w-full border-collapse text-sm tabular-nums">
      <thead className="bg-muted/50"><tr>{table.header.map((cell, index) => <th key={index} scope="col" className="border-b border-border px-3 py-2 font-semibold" style={{ textAlign: table.alignments[index] }} dangerouslySetInnerHTML={{ __html: renderInline(cell) }} />)}</tr></thead>
      <tbody>{table.rows.map((row, rowIndex) => <tr key={rowIndex} className="border-b border-border last:border-0">
        {table.header.map((_, index) => <td key={index} className="px-3 py-2 align-top" style={{ textAlign: table.alignments[index] }} dangerouslySetInnerHTML={{ __html: renderInline(row[index] ?? '') }} />)}
      </tr>)}</tbody>
    </table>
  </div>
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
    {splitMarkdownBlocks(text).map((block) => {
      const key = start
      start += block.length + 1
      return <MarkdownBlock key={key} block={block} />
    })}
  </div>
})
