import assert from 'node:assert/strict'
import { test } from 'node:test'

import { composeSoul, draftSoul, editSoul, fillCreationDetails, renameSoul, soulFields } from '../src/lib/agent-soul.ts'

const scaffold = [
  '# Soul - {{agent_name}}',
  '',
  '## Identity',
  '- **Role**: {{role_description}}',
  '',
  '## Personality',
  "I'm {{agent_name}}. I exist to complete tasks.",
  '',
  '## Work Style',
  '- I track the work through completion.',
  '',
  '## Boundaries',
  '- Comply with corporate confidentiality policies',
  '',
  '## Image skills',
  'Read the skills guide first.',
  '',
].join('\n')

const template = [
  '# Soul - {name}',
  '',
  '## Identity',
  '- **Role**: Risk Manager (the gatekeeper)',
  '- **Expertise**: Position sizing',
  '',
  '## Personality',
  '- Calm.',
  '',
  '## Work Style - the Stage / Guards / Push flow',
  '- Stage first.',
  '',
  '### Guards',
  '- Check limits.',
  '',
  '## Boundaries',
  '- No trades without approval.',
  '',
].join('\n')

test('Given a template soul, when its fields are read, then role and sections come back including a subtitled heading', () => {
  assert.deepEqual(soulFields(template), {
    role: 'Risk Manager (the gatekeeper)',
    personality: '- Calm.',
    workStyle: '- Stage first.\n\n### Guards\n- Check limits.',
    boundaries: '- No trades without approval.',
  })
  assert.equal(soulFields(scaffold).role, '')
})

test('Given the scaffold and its own fields, when composed, then the soul is unchanged', () => {
  assert.equal(composeSoul(scaffold, soulFields(scaffold)), scaffold)
})

test('Given template fields, when composed, then sections are written and the identity and other sections stay', () => {
  const soul = composeSoul(scaffold, soulFields(template))

  assert.equal(soul, [
    '# Soul - {{agent_name}}',
    '',
    '## Identity',
    '- **Role**: {{role_description}}',
    '',
    '## Personality',
    '- Calm.',
    '',
    '## Work Style',
    '- Stage first.',
    '',
    '### Guards',
    '- Check limits.',
    '',
    '## Boundaries',
    '- No trades without approval.',
    '',
    '## Image skills',
    'Read the skills guide first.',
    '',
  ].join('\n'))
})

test('Given empty fields, when composed, then their sections are dropped', () => {
  const soul = composeSoul(scaffold, { personality: '', workStyle: ' ', boundaries: '' })

  assert.equal(soul, [
    '# Soul - {{agent_name}}',
    '',
    '## Identity',
    '- **Role**: {{role_description}}',
    '',
    '## Image skills',
    'Read the skills guide first.',
    '',
  ].join('\n'))
  assert.equal(
    composeSoul('# Soul\n\n## Personality\nCalm.\n', { personality: '', workStyle: '', boundaries: '' }),
    '# Soul\n',
  )
})

test('Given creation placeholders, when filled, then creator and zero-padded local date replace them and other placeholders stay', () => {
  const soul = '- **Name**: {{agent_name}}\n- **Created by**: {{creator_name}}\n- **Created at**: {{created_at}}\n'
  const now = new Date(2026, 2, 5, 23, 59)

  assert.equal(
    fillCreationDetails(soul, ' Kim $& Lee ', now),
    '- **Name**: {{agent_name}}\n- **Created by**: Kim $& Lee\n- **Created at**: 2026-03-05\n',
  )
  assert.equal(
    fillCreationDetails(soul, '  ', now),
    '- **Name**: {{agent_name}}\n- **Created by**: {{creator_name}}\n- **Created at**: 2026-03-05\n',
  )
})

const identitySoul = (name: string, role: string, personality: string) => [
  `# Soul - ${name}`, '',
  '## Identity', `- **Name**: ${name}`, `- **Role**: ${role}`, '- **Created by**: Kim', '',
  '## Personality', personality, '',
].join('\n')

test('Given name and role, when drafted and renamed, then every placeholder follows them and empty values restore placeholders', () => {
  const soul = identitySoul('{{agent_name}}', '{{role_description}}', "I'm {{agent_name}}, the {{role_description}}. {{creator_name}}")

  const drafted = draftSoul(soul, { name: ' Rita $& Co ', role: 'Risk\n  analyst' })

  assert.equal(drafted.text, identitySoul('Rita $& Co', 'Risk analyst', "I'm Rita $& Co, the Risk analyst. {{creator_name}}"))
  const renamed = renameSoul(drafted, { name: 'Rex', role: 'Auditor' })
  assert.equal(renamed.text, identitySoul('Rex', 'Auditor', "I'm Rex, the Auditor. {{creator_name}}"))
  assert.equal(renameSoul(renamed, { name: '', role: ' ' }).text, soul)
  assert.equal(draftSoul('# Notes\nNo identity here.\n', { name: 'Rita', role: 'Analyst' }).text, '# Notes\nNo identity here.\n')
})

test('Given soul edits, when renamed, then names beside the edit follow, an edited name stops and identity lines are rewritten', () => {
  const drafted = draftSoul(identitySoul('{{agent_name}}', '{{role_description}}', "I'm {{agent_name}}. Ask {{agent_name}}."), { name: 'Rita', role: '' })
  const greeted = editSoul(drafted, drafted.text.replace("I'm Rita.", "Hi, I'm Rita."))
  const beside = editSoul(greeted, greeted.text.replace("I'm Rita.", "I'm Rita. Calm."))
  const touched = editSoul(beside, beside.text.replace('Ask Rita.', 'Ask Ritz.'))
  const retitled = editSoul(touched, touched.text.replace('# Soul - Rita', '# Soul - Rita the analyst'))

  assert.equal(
    renameSoul(retitled, { name: 'Rex', role: 'Auditor' }).text,
    identitySoul('Rex', 'Auditor', "Hi, I'm Rex. Calm. Ask Ritz."),
  )
})
