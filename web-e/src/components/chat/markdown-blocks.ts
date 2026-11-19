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

// A point where the scan can restart from a fresh state. `blocks` blocks precede line `line`, and the
// decision depends on no line at or after index `evidence`.
type Boundary = { readonly blocks: number; readonly line: number; readonly evidence: number }

function newlinesBetween(text: string, from: number, to: number): number {
  let count = 0
  for (let index = text.indexOf('\n', from); index >= 0 && index < to; index = text.indexOf('\n', index + 1)) count++
  return count
}

// Blank-line runs inside a paragraph end a block. A run is a boundary only when the next line is certainly a
// paragraph line: a complete line that cannot become a fence or table header. Otherwise a later fence or table
// would flush the paragraph with the run's trailing newlines still attached to the previous block.
function paragraphBoundaries(text: string, start: number, blocksBefore: number, lines: readonly string[], boundaries: Boundary[]) {
  const last = lines.length - 1
  let line = start
  let from = 0
  let pieces = 0
  for (const run of text.matchAll(/\n{2,}/g)) {
    const end = run.index + run[0].length
    if (end === text.length) break
    line += newlinesBetween(text, from, end)
    from = end
    pieces++
    const decided = tableRow(lines[line] ?? '') === null ? line : line + 1
    if (decided < last) boundaries.push({ blocks: blocksBefore + pieces, line, evidence: decided + 1 })
  }
}

// Every line before the last one is terminated by a newline; the last line may still grow.
function scanBlocks(lines: readonly string[], boundaries?: Boundary[]): string[] {
  const last = lines.length - 1
  const blocks: string[] = []
  let paragraph: string[] = []
  let paragraphStart = 0
  const flush = () => {
    if (paragraph.length) {
      const text = paragraph.join('\n')
      if (boundaries) paragraphBoundaries(text, paragraphStart, blocks.length, lines, boundaries)
      blocks.push(...text.split(/\n{2,}/))
    }
    paragraph = []
  }
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index] ?? ''
    const fence = /^\s*(`{3,})([^`]*)$/.exec(line)
    if (fence) {
      flush()
      const code = [line]
      const closing = new RegExp(`^\\s*\x60{${fence[1]?.length ?? 3},}\\s*$`)
      let closed = false
      while (++index < lines.length) {
        const next = lines[index] ?? ''
        code.push(next)
        if (closing.test(next)) {
          closed = true
          break
        }
      }
      blocks.push(code.join('\n'))
      if (closed && index < last) boundaries?.push({ blocks: blocks.length, line: index + 1, evidence: index + 1 })
    } else if (parseMarkdownTable(`${line}\n${lines[index + 1] ?? ''}`)) {
      flush()
      const table = [line, lines[++index] ?? '']
      while (index + 1 < lines.length && tableRow(lines[index + 1] ?? '')) table.push(lines[++index] ?? '')
      blocks.push(table.join('\n'))
      if (index + 1 < last) boundaries?.push({ blocks: blocks.length, line: index + 1, evidence: index + 2 })
    } else {
      if (!paragraph.length) paragraphStart = index
      paragraph.push(line)
    }
  }
  flush()
  return blocks.length ? blocks : ['']
}

export function splitMarkdownBlocks(text: string): readonly string[] {
  return scanBlocks(text.replaceAll('\r\n', '\n').split('\n'))
}

export type MarkdownBlocks = {
  readonly blocks: readonly string[]
  // Leading blocks no later text can change; they are never scanned again while the text only grows.
  readonly completed: number
  // Lines examined by the update that produced this result.
  readonly scanned: number
}

type Mark = { readonly blocks: number; readonly offset: number; readonly evidence: number }

function commonPrefix(a: string, b: string): number {
  const limit = Math.min(a.length, b.length)
  let index = 0
  while (index < limit && a.charCodeAt(index) === b.charCodeAt(index)) index++
  return index
}

export type MarkdownSplitter = ReturnType<typeof createMarkdownSplitter>

// Splits growing text into the same blocks as `splitMarkdownBlocks`, reparsing only what later text can still change.
// A mark is a boundary at a line start; `evidence` is the end of the last line that decided it, so a mark stays valid
// for any text that keeps the first `evidence` characters.
export function createMarkdownSplitter() {
  let source = ''
  let frozen: readonly string[] = []
  const marks: Mark[] = []
  let result: MarkdownBlocks | null = null
  return {
    update(text: string): MarkdownBlocks {
      if (result && text === source) return result
      if (!text.startsWith(source)) {
        const unchanged = commonPrefix(source, text)
        while (marks.length && (marks.at(-1)?.evidence ?? 0) > unchanged) marks.pop()
        frozen = frozen.slice(0, marks.at(-1)?.blocks ?? 0)
      }
      const origin = marks.at(-1)?.offset ?? 0
      const raw = text.slice(origin).split('\n')
      const starts: number[] = []
      let position = origin
      for (const line of raw) {
        starts.push(position)
        position += line.length + 1
      }
      const lines = raw.map((line, index) => index < raw.length - 1 && line.endsWith('\r') ? line.slice(0, -1) : line)
      const boundaries: Boundary[] = []
      const tail = scanBlocks(lines, boundaries)
      for (const boundary of boundaries) {
        marks.push({ blocks: frozen.length + boundary.blocks, offset: starts[boundary.line] ?? origin, evidence: starts[boundary.evidence] ?? origin })
      }
      const advance = boundaries.at(-1)?.blocks ?? 0
      frozen = frozen.concat(tail.slice(0, advance))
      source = text
      result = { blocks: frozen.concat(tail.slice(advance)), completed: frozen.length, scanned: lines.length }
      return result
    },
  }
}
