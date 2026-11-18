import assert from 'node:assert/strict'
import test from 'node:test'
import { createMarkdownSplitter, parseMarkdownTable, splitMarkdownBlocks } from '../src/components/chat/markdown-blocks.ts'

test('tables interrupt adjacent paragraphs and normalize CRLF without splitting fenced code', () => {
  const text = '**Key directories:**\r\n| Directory | Purpose |\r\n| --- | --- |\r\n| engine/ | API |\r\nAfter table\r\n\r\n```ts\r\na|b\r\n\r\nc|d\r\n```'
  const blocks = splitMarkdownBlocks(text)
  assert.equal(blocks[0], '**Key directories:**')
  assert.deepEqual(parseMarkdownTable(blocks[1] ?? ''), { header: ['Directory', 'Purpose'], alignments: ['left', 'left'], rows: [['engine/', 'API']] })
  assert.equal(blocks[2]?.trim(), 'After table')
  assert.equal(blocks.at(-1), '```ts\na|b\n\nc|d\n```')
})

test('table cells retain inline markup and literal escaped or code pipes with delimiter alignment', () => {
  const table = parseMarkdownTable('Left | Middle | Right\n:--- | :---: | ---:\n**bold** \\| value | `a|b` | [link](https://example.test)\n``a|`b`` | short')
  assert.deepEqual(table, {
    header: ['Left', 'Middle', 'Right'], alignments: ['left', 'center', 'right'],
    rows: [['**bold** | value', '`a|b`', '[link](https://example.test)'], ['``a|`b``', 'short']],
  })
})

test('malformed tables remain paragraphs and fenced table syntax remains code', () => {
  for (const text of ['a | b\n--- | --', 'a | b\n---', 'a | b\n--- | nope', '`a|b`\n--- | ---']) {
    assert.equal(parseMarkdownTable(text), null)
    assert.deepEqual(splitMarkdownBlocks(text), [text])
  }
  const code = '```md\n| a | b |\n| --- | --- |\n\n| x | y |\n```'
  assert.deepEqual(splitMarkdownBlocks(code), [code])
})

const report = [
  '# Quarterly report', '',
  'Intro line one', 'Intro line two with `a|b` and **bold**', '',
  '| Region | Revenue | Notes |', '| :--- | ---: | :---: |', '| North | 10 | `x|y` |', '| South | 20 | a \\| b |', 'After the table', '',
  '- first', '- **second**', '', '## Details', '',
  'Pipe | not a table', 'plain follow-up', '',
  '```ts', 'const a = 1', '', '', 'const b = 2', '```', '',
  'Closing paragraph', '', '', 'Another after a double blank', '',
  '```', 'unmatched fence', '', 'still code', '', '',
]

function chunked(text: string, size: number): string[] {
  const parts: string[] = []
  for (let index = 0; index < text.length; index += size) parts.push(text.slice(index, index + size))
  return parts
}

function assertStreamMatches(parts: readonly string[]) {
  const splitter = createMarkdownSplitter()
  let text = ''
  for (const part of parts) {
    text += part
    assert.deepEqual(splitter.update(text).blocks, splitMarkdownBlocks(text), JSON.stringify(text))
  }
}

for (const [name, separator] of [['LF', '\n'], ['CRLF', '\r\n']] as const) {
  const text = report.join(separator)
  test(`streaming a ${name} reply in any chunk size gives the blocks of the full text at every step`, () => {
    for (const size of [1, 2, 3, 5, 7, 13, 64]) assertStreamMatches(chunked(text, size))
  })

  test(`splitting a ${name} reply at every character boundary gives the blocks of the full text`, () => {
    for (let at = 0; at <= text.length; at++) {
      const splitter = createMarkdownSplitter()
      assert.deepEqual(splitter.update(text.slice(0, at)).blocks, splitMarkdownBlocks(text.slice(0, at)), `prefix ${at}`)
      assert.deepEqual(splitter.update(text).blocks, splitMarkdownBlocks(text), `split ${at}`)
    }
  })
}

test('a CRLF split between chunks joins into one line break', () => {
  assertStreamMatches(['one\r', '\n\r', '\ntwo\r', '\n'])
  const splitter = createMarkdownSplitter()
  assert.deepEqual(splitter.update('one\r\n\r').blocks, ['one\n\r'])
  assert.deepEqual(splitter.update('one\r\n\r\n').blocks, ['one', ''])
})

test('a pipe line becomes a table header once its delimiter row arrives', () => {
  const splitter = createMarkdownSplitter()
  assert.equal(parseMarkdownTable(splitter.update('Name | Qty\n').blocks[0] ?? ''), null)
  assert.equal(parseMarkdownTable(splitter.update('Name | Qty\n--- | --').blocks[0] ?? ''), null)
  const table = splitter.update('Name | Qty\n--- | ---\n')
  assert.deepEqual(parseMarkdownTable(table.blocks[0] ?? ''), { header: ['Name', 'Qty'], alignments: ['left', 'left'], rows: [] })
  assert.deepEqual(splitter.update('Name | Qty\n--- | ---\nbolt | 4\nplain').blocks, splitMarkdownBlocks('Name | Qty\n--- | ---\nbolt | 4\nplain'))
})

test('a header that turns into a table reaches back over the blank line before it', () => {
  for (const text of [
    'a\n\nName | Qty\n--- | ---\nbolt | 4', 'a\n\n\nName | Qty\n--- | ---\nbolt | 4', 'a\n\n```\ncode', 'a\n\n\n\n```\ncode\n```\nz',
    'a\n\nName | Qty\n--- | ---x', 'a\n\n\nName | Qty\n--- | ---x', 'a\n\n\nName | Qty\n--- | ---\nrow | 1\nend',
  ]) {
    assertStreamMatches(chunked(text, 1))
  }
  const splitter = createMarkdownSplitter()
  splitter.update('a\n\nName | Qty\n')
  assert.deepEqual(splitter.update('a\n\nName | Qty\n--- | ---\n').blocks.slice(0, 1), ['a\n'])
})

test('blank lines inside an open fence stay code, even when the fence never closes', () => {
  const splitter = createMarkdownSplitter()
  for (const text of ['```ts\nA\n', '```ts\nA\n\n', '```ts\nA\n\n\nB\n\n']) assert.deepEqual(splitter.update(text).blocks, [text])
  const closed = splitter.update('```ts\nA\n\n\nB\n\n```\n\nafter')
  assert.deepEqual(closed.blocks, ['```ts\nA\n\n\nB\n\n```', '\nafter'])
  assert.deepEqual(closed.blocks, splitMarkdownBlocks('```ts\nA\n\n\nB\n\n```\n\nafter'))
})

function build(paragraphs: number, tail: string) {
  return `${Array.from({ length: paragraphs }, (_, index) => `Paragraph ${index}`).join('\n\n')}\n\n${tail}`
}

test('appending to a long reply scans only the mutable tail, whatever the number of completed blocks', () => {
  const scans = new Map<number, number[]>()
  for (const paragraphs of [50, 500]) {
    const splitter = createMarkdownSplitter()
    let text = build(paragraphs, 'Tail')
    const first = splitter.update(text)
    assert.equal(first.scanned, paragraphs * 2 + 1)
    const observed: number[] = []
    let previous = first
    for (const chunk of [' grows', ' more', '\n\n', 'Next paragraph', '\nsecond line', '\n\n\nAfter']) {
      text += chunk
      const next = splitter.update(text)
      assert.deepEqual(next.blocks, splitMarkdownBlocks(text))
      assert.ok(next.completed >= previous.completed)
      assert.deepEqual(next.blocks.slice(0, previous.completed), previous.blocks.slice(0, previous.completed))
      observed.push(next.scanned)
      previous = next
    }
    assert.ok(Math.max(...observed) <= 8, `tail scans ${observed}`)
    assert.ok(previous.completed >= paragraphs, 'earlier paragraphs are completed')
    scans.set(paragraphs, observed)
  }
  assert.deepEqual(scans.get(50), scans.get(500))
})

test('completed table and fence blocks are not scanned again once a following line is complete', () => {
  const splitter = createMarkdownSplitter()
  const rows = Array.from({ length: 200 }, (_, index) => `| row ${index} | ${index} |`)
  let text = ['Intro', '', '| Name | Qty |', '| --- | --- |', ...rows, 'After the table', '', '```', ...rows, '```', ''].join('\n')
  const first = splitter.update(text)
  assert.equal(first.completed, 4)
  for (const chunk of ['more', ' prose', '\n\nfinal']) {
    text += chunk
    const next = splitter.update(text)
    assert.deepEqual(next.blocks, splitMarkdownBlocks(text))
    assert.ok(next.scanned <= 5, `scanned ${next.scanned}`)
  }
})

test('the open tail is rescanned as a whole while its fence or table keeps growing', () => {
  const splitter = createMarkdownSplitter()
  const code = Array.from({ length: 40 }, (_, index) => `line ${index}`).join('\n\n')
  const head = 'First\n\nIntro\n\n```\n'
  const opened = splitter.update(`${head}${code}\n\n`)
  assert.equal(opened.completed, 1)
  const grown = splitter.update(`${head}${code}\n\nmore\n\n`)
  assert.equal(grown.completed, 1)
  assert.equal(grown.blocks.length, 3)
  assert.ok(grown.scanned > 80, `scanned ${grown.scanned}`)
})

test('an update with the same text returns the same result object', () => {
  const splitter = createMarkdownSplitter()
  const first = splitter.update('a\n\nb\n\nc')
  assert.equal(splitter.update('a\n\nb\n\nc'), first)
})

test('replacing the first paragraph reparses from the start', () => {
  const splitter = createMarkdownSplitter()
  const original = build(120, 'Tail')
  const lines = splitter.update(original).scanned
  const replaced = original.replace('Paragraph 0', 'Rewritten opening')
  const next = splitter.update(replaced)
  assert.deepEqual(next.blocks, splitMarkdownBlocks(replaced))
  assert.equal(next.blocks[0], 'Rewritten opening')
  assert.equal(next.scanned, lines)
})

test('replacing only the end of a reply reuses the unchanged completed prefix', () => {
  const splitter = createMarkdownSplitter()
  const original = build(300, 'Draft tail\nwith two lines')
  splitter.update(original)
  const revised = build(300, 'Final tail\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n')
  const next = splitter.update(revised)
  assert.deepEqual(next.blocks, splitMarkdownBlocks(revised))
  assert.ok(next.scanned <= 10, `scanned ${next.scanned}`)
  assert.ok(next.completed >= 299)
})

test('replacing a middle paragraph reparses from just before the change', () => {
  const splitter = createMarkdownSplitter()
  const original = build(300, 'Tail')
  const total = splitter.update(original).scanned
  const revised = original.replace('Paragraph 150', 'Edited paragraph\n\nplus another')
  const next = splitter.update(revised)
  assert.deepEqual(next.blocks, splitMarkdownBlocks(revised))
  assert.ok(next.scanned >= 300 && next.scanned < total, `scanned ${next.scanned} of ${total}`)
})

test('a replacement never reuses a boundary whose deciding lines changed', () => {
  const splitter = createMarkdownSplitter()
  assert.deepEqual(splitter.update('a\n\nplain line\nmore\n').blocks, ['a', 'plain line\nmore\n'])
  const next = splitter.update('a\n\n```\nplain line\nmore\n')
  assert.deepEqual(next.blocks, ['a\n', '```\nplain line\nmore\n'])
  assert.deepEqual(next.blocks, splitMarkdownBlocks('a\n\n```\nplain line\nmore\n'))
})

test('replacing with shorter or empty text rewinds to the surviving prefix', () => {
  const splitter = createMarkdownSplitter()
  const original = build(40, 'Tail')
  splitter.update(original)
  for (const text of [build(10, 'Short'), 'Paragraph 0', '', original, 'x']) {
    assert.deepEqual(splitter.update(text).blocks, splitMarkdownBlocks(text))
  }
})

function seeded(seed: number): () => number {
  let state = seed
  return () => {
    state = (state + 0x6d2b79f5) | 0
    let mixed = Math.imul(state ^ (state >>> 15), 1 | state)
    mixed = (mixed + Math.imul(mixed ^ (mixed >>> 7), 61 | mixed)) ^ mixed
    return ((mixed ^ (mixed >>> 14)) >>> 0) / 4294967296
  }
}

test('random appends and replacements always match splitting the full text', () => {
  const fragments = ['a', 'b c', '', '', '', '| h | k |', '| --- | --- |', '| :-: | --: |', 'x | y', '---|---', '```', '```ts', '  ```', '````', 'tail \\| pipe', '`a|b`', '- item', '# Heading', ' ']
  const separators = ['\n', '\n', '\n', '\r\n', '\r']
  for (let seed = 1; seed <= 250; seed++) {
    const random = seeded(seed)
    const pick = <T>(items: readonly T[]): T => items[Math.floor(random() * items.length)] as T
    const piece = () => `${pick(fragments)}${pick(separators)}`
    const splitter = createMarkdownSplitter()
    let text = ''
    for (let step = 0; step < 30; step++) {
      const action = random()
      if (action < 0.7) text += Array.from({ length: Math.floor(random() * 3) }, piece).join('').slice(0, Math.floor(random() * 24) + 1)
      else if (action < 0.9) {
        const start = Math.floor(random() * (text.length + 1))
        text = text.slice(0, start) + Array.from({ length: Math.floor(random() * 4) }, piece).join('')
      } else text = text.slice(0, Math.floor(random() * (text.length + 1)))
      assert.deepEqual(splitter.update(text).blocks, splitMarkdownBlocks(text), `seed ${seed} step ${step}: ${JSON.stringify(text)}`)
    }
  }
})
