import assert from 'node:assert/strict'
import test from 'node:test'
import { configFields, fieldOperations } from '../src/pages/tools-config-fields.ts'

const fields = configFields({ fields: [
  { key: 'api_key', label: 'API key', type: 'password' },
  { key: 'region', label: 'Region', type: 'text' },
  { key: 'port', label: 'Port', type: 'number' },
] })

test('field edits preserve omitted secrets and remove only an explicitly selected key', () => {
  assert.deepEqual(fieldOperations(fields, { region: { operation: 'set', value: 'eu-west-2' }, port: { operation: 'unset', value: '' } }), { set: { region: 'eu-west-2' }, unset: ['port'] })
  assert.deepEqual(fieldOperations(fields, {}), { set: {}, unset: [] })
})

test('field edits reject masked and empty secrets and parse numeric values', () => {
  for (const value of ['********', '', '  ']) assert.throws(() => fieldOperations(fields, { api_key: { operation: 'set', value } }))
  assert.deepEqual(fieldOperations(fields, { port: { operation: 'set', value: '993' } }), { set: { port: 993 }, unset: [] })
  assert.throws(() => fieldOperations(fields, { port: { operation: 'set', value: 'invalid' } }))
})
