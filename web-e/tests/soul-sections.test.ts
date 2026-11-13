import assert from 'node:assert/strict'
import { test } from 'node:test'

import { replaceSoulSection, soulSection } from '../src/lib/soul-sections.ts'

const soul = [
  '# Soul - {{agent_name}}',
  '',
  '## Personality',
  "I'm {{agent_name}}. I exist to complete tasks.",
  '',
  '## Core Traits',
  '- **Persistent**: I do not give up.',
  '',
  '## Boundaries',
  '- Comply with corporate confidentiality policies',
  '- Sensitive actions require creator approval',
  '',
].join('\n')

test('Given a soul, when a section is read, then its trimmed body stops at the next heading', () => {
  assert.equal(soulSection(soul, 'Personality'), "I'm {{agent_name}}. I exist to complete tasks.")
  assert.equal(soulSection(soul, 'boundaries'), '- Comply with corporate confidentiality policies\n- Sensitive actions require creator approval')
  assert.equal(soulSection(soul, 'Work style'), null)
})

test('Given a middle section, when it is replaced, then the following sections stay intact after one blank line', () => {
  assert.equal(replaceSoulSection(soul, 'Personality', 'Personality: warm. Work style: concise.'), [
    '# Soul - {{agent_name}}',
    '',
    '## Personality',
    'Personality: warm. Work style: concise.',
    '',
    '## Core Traits',
    '- **Persistent**: I do not give up.',
    '',
    '## Boundaries',
    '- Comply with corporate confidentiality policies',
    '- Sensitive actions require creator approval',
    '',
  ].join('\n'))
})

test('Given the last section, when it is replaced, then the text ends with the new body and one newline', () => {
  const replaced = replaceSoulSection(soul, 'Boundaries', 'No purchases.')

  assert.equal(replaced, soul.replace(/## Boundaries[\s\S]*$/, '## Boundaries\nNo purchases.\n'))
  assert.equal(soulSection(replaced, 'Core Traits'), '- **Persistent**: I do not give up.')
})

test('Given a soul without the section, when it is replaced, then the section is appended', () => {
  assert.equal(
    replaceSoulSection('# Soul\n\n## Personality\nCalm.\n', 'Boundaries', 'No purchases.'),
    '# Soul\n\n## Personality\nCalm.\n\n## Boundaries\nNo purchases.\n',
  )
})
