type Alignment = 'left' | 'center' | 'right'
export type MarkdownTable = {
  readonly header: readonly string[]
  readonly alignments: readonly Alignment[]
  readonly rows: readonly (readonly string[])[]
}

function tableRow(line: string): readonly string[] | null {
  const cells: string[] = []
  let cell = ''
  let code = ''
  let separators = 0
  const source = line.trim()
  for (let index = 0; index < source.length; index++) {
    const character = source[index]
    if (character === '\\' && index + 1 < source.length) {
      const next = source[++index]
      cell += next === '|' ? '|' : `\\${next}`
    } else if (character === '`') {
      const run = /^`+/.exec(source.slice(index))?.[0] ?? '`'
      if (code === run) code = ''
      else if (!code && source.indexOf(run, index + run.length) >= 0) code = run
      cell += run
      index += run.length - 1
    } else if (character === '|' && !code) {
      cells.push(cell.trim())
      cell = ''
      separators++
    } else cell += character
  }
  if (!separators) return null
  cells.push(cell.trim())
  if (source.startsWith('|')) cells.shift()
  if (source.endsWith('|') && cell === '') cells.pop()
  return cells
}

export function parseMarkdownTable(block: string): MarkdownTable | null {
  const [heading = '', delimiter = '', ...body] = block.split('\n')
  const header = tableRow(heading)
  const markers = tableRow(delimiter)
  if (!header?.length || !markers || header.length !== markers.length || !markers.every((cell) => /^:?-{3,}:?$/.test(cell))) return null
  const alignments = markers.map((cell): Alignment => cell.startsWith(':') && cell.endsWith(':') ? 'center' : cell.endsWith(':') ? 'right' : 'left')
  return { header, alignments, rows: body.map((line) => tableRow(line) ?? [line]) }
}

export function splitMarkdownBlocks(text: string): readonly string[] {
  const lines = text.replaceAll('\r\n', '\n').split('\n')
  const blocks: string[] = []
  let paragraph: string[] = []
  const flush = () => {
    if (paragraph.length) blocks.push(...paragraph.join('\n').split(/\n{2,}/))
    paragraph = []
  }
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index] ?? ''
    const fence = /^\s*(`{3,})([^`]*)$/.exec(line)
    if (fence) {
      flush()
      const code = [line]
      const closing = new RegExp(`^\\s*\x60{${fence[1]?.length ?? 3},}\\s*$`)
      while (++index < lines.length) {
        const next = lines[index] ?? ''
        code.push(next)
        if (closing.test(next)) break
      }
      blocks.push(code.join('\n'))
    } else if (parseMarkdownTable(`${line}\n${lines[index + 1] ?? ''}`)) {
      flush()
      const table = [line, lines[++index] ?? '']
      while (index + 1 < lines.length && tableRow(lines[index + 1] ?? '')) table.push(lines[++index] ?? '')
      blocks.push(table.join('\n'))
    } else paragraph.push(line)
  }
  flush()
  return blocks.length ? blocks : ['']
}
