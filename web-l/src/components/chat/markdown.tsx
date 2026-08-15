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

export function ChatMarkdown({ text }: { text: string }) {
  const html = text
    .split(/\n{2,}/)
    .map((block) => {
      const trimmed = block.trim()
      if (trimmed.startsWith('```')) {
        const body = trimmed.replace(/^```[a-zA-Z]*\n?/, '').replace(/```$/, '')
        return `<pre class="overflow-x-auto rounded-lg bg-background/80 p-2 text-xs"><code>${escapeHtml(body)}</code></pre>`
      }
      if (trimmed.startsWith('# ')) return `<h3 class="font-semibold">${renderInline(trimmed.slice(2))}</h3>`
      if (trimmed.startsWith('## ')) return `<h4 class="font-semibold">${renderInline(trimmed.slice(3))}</h4>`
      if (/^[-*] /.test(trimmed)) {
        const items = trimmed
          .split('\n')
          .filter((line) => /^[-*] /.test(line))
          .map((line) => `<li>${renderInline(line.replace(/^[-*] /, ''))}</li>`)
          .join('')
        return `<ul class="list-disc space-y-0.5 pl-5">${items}</ul>`
      }
      return `<p>${renderInline(block).replaceAll('\n', '<br />')}</p>`
    })
    .join('')

  return <div className="space-y-2 [&_a]:text-inherit" dangerouslySetInnerHTML={{ __html: html }} />
}
