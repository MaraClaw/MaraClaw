import assert from 'node:assert/strict'
import test from 'node:test'
import { parseMarkdownTable, splitMarkdownBlocks } from '../src/components/chat/markdown-blocks.ts'

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
