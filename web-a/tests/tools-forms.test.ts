import assert from 'node:assert/strict'
import test from 'node:test'

import { configPathLabel, configurationDraft, configurationInput, configurationSchema } from '../src/pages/tools-config.ts'
import { createInput, jsonObjectText, metadataInput, toolCreateSchema, toolMcpMetadataSchema, toolMetadataSchema, toolsQueryKey, toolsTenantId } from '../src/pages/tools-forms.ts'
import type { UserOut } from '../src/lib/types/auth.ts'

const orgAdmin: UserOut = {
  id: 'admin', identity_id: 'identity', email: 'admin@example.test', display_name: 'Admin', avatar_url: null,
  role: 'org_admin', is_platform_admin: false, tenant_id: 'own-company', title: null, primary_mobile: null,
  registration_source: null, is_active: true, email_verified: true, created_at: '',
}
const editorValues = {
  name: 'search', category: 'custom', display_name: ' Search ', description: 'Search tools',
  mcp_server_url: 'https://mcp.example.test', mcp_server_name: 'Search server', mcp_tool_name: 'lookup',
  parameters_schema: '{"type":"object","properties":{}}', enabled: false, is_default: true,
}

test('organization scope ignores a forged company query', () => {
  // Given
  const company = 'foreign-company'
  // When
  const tenant = toolsTenantId(orgAdmin, company)
  // Then
  assert.equal(tenant, 'own-company')
})

test('platform scope honors a selected company rather than its membership', () => {
  // Given
  const user: UserOut = { ...orgAdmin, role: 'platform_admin' }
  // When
  const tenant = toolsTenantId(user, 'selected-company')
  // Then
  assert.equal(tenant, 'selected-company')
})

test('identity platform flag permits company selection', () => {
  // Given
  const user = { ...orgAdmin, is_platform_admin: true }
  // When
  const tenant = toolsTenantId(user, 'selected-company')
  // Then
  assert.equal(tenant, 'selected-company')
})

test('platform current scope does not silently select its own company', () => {
  // Given
  const user: UserOut = { ...orgAdmin, role: 'platform_admin' }
  // When
  const tenant = toolsTenantId(user, null)
  // Then
  assert.equal(tenant, undefined)
})

test('catalog query keys separate every company and current scope', () => {
  // Given
  const scopes = ['one', 'two', undefined]
  // When
  const keys = scopes.map(toolsQueryKey)
  // Then
  assert.equal(new Set(keys.map((key) => JSON.stringify(key))).size, 3)
})

test('metadata payload omits config and immutable fields even when supplied to the form', () => {
  // Given
  const values = toolMcpMetadataSchema.parse({ ...editorValues, name: '', category: '', mcp_tool_name: null, config: { api_key: 'do-not-send' }, type: 'mcp' })
  // When
  const payload = metadataInput(values, 'company-a')
  // Then
  assert.deepEqual(payload, {
    display_name: 'Search', description: 'Search tools', mcp_server_url: 'https://mcp.example.test',
    mcp_server_name: 'Search server', parameters_schema: { type: 'object', properties: {} },
    enabled: false, is_default: true, tenant_id: 'company-a',
  })
})

test('metadata editing accepts only visible details without create-only or server fields', () => {
  // Given
  const values = {
    display_name: 'Built-in search', description: '', parameters_schema: '{}', enabled: true, is_default: false,
  }
  // When
  const result = toolMetadataSchema.safeParse(values)
  // Then
  assert.equal(result.success, true)
})

test('metadata editing ignores hidden immutable and non-MCP server values', () => {
  // Given
  const values = {
    ...editorValues, name: '', category: '', mcp_tool_name: null,
    mcp_server_url: 'not-an-http-url', mcp_server_name: null,
  }
  // When
  const result = toolMetadataSchema.safeParse(values)
  // Then
  assert.equal(result.success, true)
  if (!result.success) return
  assert.deepEqual(metadataInput(result.data, 'company-a'), {
    display_name: 'Search', description: 'Search tools', parameters_schema: { type: 'object', properties: {} },
    enabled: false, is_default: true, tenant_id: 'company-a',
  })
})

test('MCP metadata editing validates the visible server URL', () => {
  // Given
  const values = { ...editorValues, mcp_server_url: 'file:///private/server' }
  // When
  const result = toolMcpMetadataSchema.safeParse(values)
  // Then
  assert.equal(result.success, false)
  if (result.success) return
  assert.deepEqual(result.error.issues.map((issue) => issue.path), [['mcp_server_url']])
})

test('MCP metadata editing can clear optional connection metadata without create-only validation', () => {
  // Given
  const values = { ...editorValues, name: '', category: '', mcp_tool_name: '', mcp_server_url: '', mcp_server_name: '' }
  // When
  const parsed = toolMcpMetadataSchema.parse(values)
  // Then
  assert.deepEqual(metadataInput(parsed), {
    display_name: 'Search', description: 'Search tools', parameters_schema: { type: 'object', properties: {} },
    enabled: false, is_default: true, mcp_server_url: null, mcp_server_name: null,
  })
})

test('creation emits an MCP definition with explicit company and no config', () => {
  // Given
  const values = toolCreateSchema.parse(editorValues)
  // When
  const payload = createInput(values, 'company-b')
  // Then
  assert.deepEqual(payload, {
    name: 'search', display_name: 'Search', description: 'Search tools', category: 'custom',
    mcp_server_url: 'https://mcp.example.test', mcp_server_name: 'Search server', mcp_tool_name: 'lookup',
    parameters_schema: { type: 'object', properties: {} }, is_default: true, type: 'mcp', tenant_id: 'company-b',
  })
})

for (const field of ['name', 'display_name', 'category', 'mcp_server_url', 'mcp_server_name', 'mcp_tool_name'] as const) {
  test(`creation rejects missing ${field}`, () => {
    // Given
    const values = { ...editorValues, [field]: '   ' }
    // When
    const result = toolCreateSchema.safeParse(values)
    // Then
    assert.equal(result.success, false)
  })
}

for (const value of ['[]', 'null', '42', '"text"', '{invalid']) {
  test(`JSON object parser rejects ${value}`, () => {
    // Given / When
    const result = jsonObjectText.safeParse(value)
    // Then
    assert.equal(result.success, false)
  })
}

test('draft strips known, schema-defined, unknown masked and nested secrets without losing false or zero', () => {
  // Given
  const tool = {
    config: {
      api_key: '****1234', private_key: 'plaintext-must-not-prefill', auth_code: '****', password: '****pass', secret: '****abcd',
      custom_token: 'unmasked-private', unknown: '****5678', retries: 0, enabled: false, endpoint: 'https://example.test',
      nested: { password: '****nest', visible: 'kept' }, entries: ['****hidden'],
    },
    config_schema: { fields: [{ key: 'custom_token', type: 'password' }] },
  }
  // When
  const draft = configurationDraft(tool)
  // Then
  assert.deepEqual(JSON.parse(draft.text), {
    retries: 0, enabled: false, endpoint: 'https://example.test', nested: { visible: 'kept' }, entries: [null],
  })
  assert.deepEqual(draft.required.map(configPathLabel), [
    '/api_key', '/private_key', '/auth_code', '/password', '/secret', '/custom_token', '/unknown', '/nested/password', '/entries/0',
  ])
})

for (const replacement of [{}, { api_key: null }, { api_key: ' ' }, { api_key: '****old' }]) {
  test(`replacement requires actual secret reentry for ${JSON.stringify(replacement)}`, () => {
    // Given
    const schema = configurationSchema([['api_key']])
    // When
    const result = schema.safeParse({ config: JSON.stringify(replacement), confirmed: true })
    // Then
    assert.equal(result.success, false)
  })
}

test('replacement rejects masks even for new nested keys', () => {
  // Given
  const schema = configurationSchema([])
  // When
  const result = schema.safeParse({ config: '{"nested":[{"token":"****masked"}]}', confirmed: true })
  // Then
  assert.equal(result.success, false)
})

test('replacement requires destructive confirmation', () => {
  // Given
  const schema = configurationSchema([])
  // When
  const result = schema.safeParse({ config: '{}', confirmed: false })
  // Then
  assert.equal(result.success, false)
})

test('replacement payload contains only the new full object and tenant, not omitted old values', () => {
  // Given
  const schema = configurationSchema([['api_key']])
  const values = schema.parse({ config: '{"api_key":"new-secret","enabled":false,"retries":0}', confirmed: true })
  // When
  const payload = configurationInput(values.config, 'company-a')
  // Then
  assert.deepEqual(payload, { config: { api_key: 'new-secret', enabled: false, retries: 0 }, tenant_id: 'company-a' })
})
