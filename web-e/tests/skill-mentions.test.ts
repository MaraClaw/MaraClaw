import assert from 'node:assert/strict'
import { test } from 'node:test'

import { buildSkillMentionContent, filterSkillMentions, findSkillMention, insertSkillMention, resolveSkillMentionSegments } from '../src/lib/chat/skill-mentions.ts'

const audit = { name: 'audit-review', path: 'skills/audit-review/SKILL.md' }
const report = { name: 'report', path: 'skills/report.md' }
const dotted = { name: 'report.v2', path: 'skills/report.v2/skill.md' }
const installed = [audit, report, dotted]

test('resolved segments preserve every character and repeated mentions while separating trailing punctuation', () => {
  // Given
  const draft = '  @report.v2...\t@audit-review,\n@report! @report? @unknown.  \n'
  // When
  const segments = resolveSkillMentionSegments(draft, installed)
  // Then
  assert.equal(segments.map((segment) => segment.text).join(''), draft)
  assert.deepEqual(segments.filter((segment) => segment.skill).map(({ text, skill }) => [text, skill]), [
    ['@report.v2', dotted], ['@audit-review', audit], ['@report', report], ['@report', report],
  ])
  for (const segment of segments) assert.equal(draft.slice(segment.start, segment.start + segment.text.length), segment.text)
})

test('a full installed name ending in dots takes precedence over punctuation fallback in chips and routing', () => {
  // Given
  const fullName = { name: 'report.', path: 'skills/dot-name/SKILL.md' }
  const skills = [report, fullName]
  // When
  const segments = resolveSkillMentionSegments('@report. @report...', skills)
  const envelope = buildSkillMentionContent('@report. @report...', skills)
  // Then
  assert.deepEqual(segments.filter((segment) => segment.skill).map(({ text, skill }) => [text, skill]), [
    ['@report.', fullName], ['@report', report],
  ])
  for (const skill of skills) assert.equal(envelope.content.split(JSON.stringify(skill.path)).length - 1, 1)
})

for (const draft of ['', '  Plain\ntext  ', '@ @unknown @REPORT @reporter @report.v3 @report/child',
  'member@report (@report) https://example.test/@report']) {
  test(`unresolved segments stay verbatim and unstyled: ${JSON.stringify(draft)}`, () => {
    // Given / When
    const segments = resolveSkillMentionSegments(draft, installed)
    // Then
    assert.equal(segments.map((segment) => segment.text).join(''), draft)
    assert.ok(segments.every((segment) => segment.skill === null))
  })
}

test('an unavailable installed list leaves potential chips as plain text', () => {
  // Given / When
  const segments = resolveSkillMentionSegments(' @report\n', [])
  // Then
  assert.deepEqual(segments, [{ start: 0, text: ' @report\n', skill: null }])
})

for (const [text, caret, expected] of [
  ['@', 1, { start: 0, end: 1, query: '' }],
  ['Use @audit-review, please', 7, { start: 4, end: 17, query: 'au' }],
  ['First\n@report.', 13, { start: 6, end: 13, query: 'report' }],
  ['@report and @audit-review!', 19, { start: 12, end: 25, query: 'audit-' }],
  ['Email member@report', 19, null],
  ['(@report)', 8, null],
  ['@report after', 10, null],
  ['@report.', 8, null],
  ['Plain text', 4, null],
] as const) {
  test(`mention range respects caret and whitespace in ${JSON.stringify(text)} at ${caret}`, () => {
    // Given / When
    const range = findSkillMention(text, caret)
    // Then
    assert.deepEqual(range, expected)
  })
}

test('mention filtering searches installed names case-insensitively without modifying their paths', () => {
  // Given / When
  const matches = filterSkillMentions(installed, 'REPort')
  // Then
  assert.deepEqual(matches, [report, dotted])
  assert.deepEqual(filterSkillMentions(installed, 'catalog-only'), [])
})

for (const suffix of [', please', ' please', '\nnext line', '']) {
  test(`selection replaces the whole token from its middle and preserves suffix ${JSON.stringify(suffix)}`, () => {
    // Given
    const text = `Use @au-tail${suffix}`
    const range = findSkillMention(text, 7)
    assert.ok(range)
    // When
    const inserted = insertSkillMention(text, range, audit)
    // Then
    const expectedSuffix = suffix.startsWith(' ') ? suffix : ` ${suffix}`
    assert.equal(inserted.text, `Use @audit-review${expectedSuffix}`)
    assert.equal(inserted.caret, 'Use @audit-review '.length)
    assert.equal(inserted.text.slice(inserted.caret - 1, inserted.caret), ' ')
  })
}

test('typed exact installed mentions route each unique skill file and keep the visible trimmed request', () => {
  // Given
  const draft = '  Use @audit-review, @report.v2.\nThen @audit-review and @report! Keep @unknown.  '
  // When
  const envelope = buildSkillMentionContent(draft, installed)
  // Then
  assert.equal(envelope.display_content, draft.trim())
  assert.notEqual(envelope.content, draft.trim())
  assert.ok(envelope.content.endsWith(draft.trim()))
  for (const skill of installed) assert.equal(envelope.content.split(JSON.stringify(skill.path)).length - 1, 1)
})

for (const draft of [
  '  Ordinary message\nwith another line.  ', 'Email a@report.test and member@report',
  '@unknown @reporter @report-extra @report_v2 @report/child @report.v3',
  '@report@unknown (@report) https://example.test/@report', '@REPORT',
]) {
  test(`unresolved text retains the ordinary content-only envelope: ${JSON.stringify(draft)}`, () => {
    // Given / When
    const envelope = buildSkillMentionContent(draft, installed)
    // Then
    assert.deepEqual(envelope, { content: draft.trim() })
  })
}

test('an unavailable installed list never rewrites a manually typed mention', () => {
  // Given / When
  const envelope = buildSkillMentionContent(' @report ', [])
  // Then
  assert.deepEqual(envelope, { content: '@report' })
})
