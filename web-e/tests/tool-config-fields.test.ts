import assert from 'node:assert/strict'
import test from 'node:test'
import { memberConfigFields, memberFieldOperations } from '../src/lib/tool-config-fields.ts'

const schema = { fields: [
  { key: 'api_key', label: 'API key', type: 'password' },
  { key: 'region', label: 'Region', type: 'text' },
  ...['allow_network', 'http_proxy', 'https_proxy', 'no_proxy'].map((key) => ({ key, label: key, type: 'text' })),
] }

test('member schema fields exclude privileged values from both set and unset', () => {
  const fields = memberConfigFields(schema)
  assert.deepEqual(fields.map((field) => field.key), ['api_key', 'region'])
  assert.deepEqual(memberFieldOperations(schema.fields, { http_proxy: { operation: 'set', value: 'http://proxy.test' }, no_proxy: { operation: 'unset', value: '' }, region: { operation: 'set', value: 'eu' } }), { set: { region: 'eu' }, unset: [] })
})

test('member patches preserve secrets by omission and distinguish removal from empty input', () => {
  const fields = memberConfigFields(schema)
  assert.deepEqual(memberFieldOperations(fields, { region: { operation: 'unset', value: '' } }), { set: {}, unset: ['region'] })
  for (const value of ['********', '', '  ']) assert.throws(() => memberFieldOperations(fields, { api_key: { operation: 'set', value } }))
})
