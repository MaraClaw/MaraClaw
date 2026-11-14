import { expect, test, type Page } from '@playwright/test'

import { orgDepartment, prepareOrgDepartments } from './org-departments-fixture'
import {
  defaultSoul, deferCreation, delayedAgent, replacementToken, replacementUser, tokenKey, writeFromOtherTab,
} from './session-fencing-fixture'

const newAgentPath = '/app/agents/new'
const soulFor = (name: string) => defaultSoul.replaceAll('{{agent_name}}', () => name)
const riskTemplate = {
  id: '5c4b1d36-7f0e-4f7a-9d55-2f3b8c1e6a90', name: 'Risk Manager', description: 'Gatekeeper', icon: 'RM', category: 'trading',
  is_builtin: true, default_skills: [], default_autonomy_policy: {}, capability_bullets: [], has_bootstrap: false,
  soul_template: [
    '# Soul - {name}', '', '## Identity', '- **Role**: Risk Manager', '',
    '## Personality', '- Calm.', '', '## Work Style - the Stage flow', '- Stage first.', '',
    '## Boundaries', '- No trades without approval.', '',
  ].join('\n'),
}

async function captureWrites(page: Page, status = 200, release = Promise.resolve()) {
  const writes: { method: string; body: unknown }[] = []
  await page.route('**/api/agents/', async (route) => {
    const request = route.request()
    if (request.method() === 'GET') return route.fallback()
    writes.push({ method: request.method(), body: request.postDataJSON() })
    await release
    await route.fulfill({ status, json: status === 200 ? delayedAgent : { detail: 'Selected department is no longer available' } })
  })
  return writes
}

async function chooseDepartment(page: Page) {
  await page.goto(newAgentPath)
  await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)
  await page.getByLabel('Visibility').selectOption('department')
  await expect(page.getByLabel('Department (required)')).toBeEnabled()
  await page.getByLabel('Department (required)').selectOption(orgDepartment.id)
}

for (const { label, name, valid } of [
  { label: 'empty', name: '', valid: false },
  { label: 'whitespace only', name: '   ', valid: false },
  { label: 'one trimmed character', name: ' x ', valid: false },
  { label: 'over 100 characters', name: 'x'.repeat(101), valid: false },
  { label: 'two trimmed characters', name: ' ab ', valid: true },
  { label: '100 trimmed characters', name: ` ${'x'.repeat(100)} `, valid: true },
]) {
  test(`updates Create readiness for a name with ${label}`, async ({ page }) => {
    await prepareOrgDepartments(page)
    const writes = await captureWrites(page)
    await page.goto(newAgentPath)
    await page.getByLabel('Name', { exact: true }).fill(name)
    const create = page.getByRole('button', { name: 'Create', exact: true })

    if (valid) {
      await expect(create).toBeEnabled()
      await create.click()
      await expect(page).toHaveURL(`/app/agents/${delayedAgent.id}/chat`)
      expect(writes).toEqual([{ method: 'POST', body: {
        name: name.trim(), role_description: '', permission_scope_type: 'user',
        permission_access_level: 'use', gogcli_enabled: true, soul: soulFor(name.trim()),
      } }])
    } else {
      await expect(create).toBeDisabled()
      await expect(page.locator('#create-disabled-reason')).toBeVisible()
      await page.locator('form').dispatchEvent('submit')
      await expect(page.getByLabel('Name', { exact: true })).toBeFocused()
      expect(writes).toEqual([])
    }
  })
}

test('updates Create readiness immediately at the soul length boundary', async ({ page }) => {
  await prepareOrgDepartments(page)
  await page.goto(newAgentPath)
  await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)
  const create = page.getByRole('button', { name: 'Create', exact: true })
  await expect(create).toBeEnabled()

  await page.getByLabel('Soul', { exact: true }).fill('x'.repeat(20001))

  await expect(create).toBeDisabled()
  await expect(page.locator('#create-disabled-reason')).toBeVisible()
  await page.getByLabel('Soul', { exact: true }).fill('x'.repeat(20000))
  await expect(create).toBeEnabled()
})

test('updates Create readiness immediately at the optional role length boundary', async ({ page }) => {
  await prepareOrgDepartments(page)
  await page.goto(newAgentPath)
  await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)
  const create = page.getByRole('button', { name: 'Create', exact: true })
  await expect(create).toBeEnabled()

  await page.getByLabel('Role', { exact: true }).fill('x'.repeat(501))

  await expect(create).toBeDisabled()
  await expect(page.locator('#create-disabled-reason')).toBeVisible()
  await page.getByLabel('Role', { exact: true }).fill('x'.repeat(500))
  await expect(create).toBeEnabled()
  await page.getByLabel('Role', { exact: true }).fill('')
  await expect(create).toBeEnabled()
})

test('requires a department again when a selection is cleared and only for department visibility', async ({ page }) => {
  await prepareOrgDepartments(page)
  await chooseDepartment(page)
  const create = page.getByRole('button', { name: 'Create', exact: true })
  await expect(create).toBeEnabled()

  await page.getByLabel('Department (required)').selectOption('')

  await expect(create).toBeDisabled()
  await expect(page.locator('#create-disabled-reason')).toBeVisible()
  for (const visibility of ['private', 'company']) {
    await page.getByLabel('Visibility').selectOption(visibility)
    await expect(create).toBeEnabled()
    await page.getByLabel('Visibility').selectOption('department')
    await expect(create).toBeDisabled()
  }
  await page.getByLabel('Department (required)').selectOption(orgDepartment.id)
  await expect(create).toBeEnabled()
})

test('starts from the default soul, replaces the soul per template and sends the edited soul', async ({ page }) => {
  await prepareOrgDepartments(page)
  await page.route('**/api/agents/templates', (route) => route.fulfill({ json: [riskTemplate] }))
  const writes = await captureWrites(page)
  await page.goto(newAgentPath)
  const template = page.getByLabel('Template', { exact: true })
  const soul = page.getByLabel('Soul', { exact: true })

  await expect(template).toHaveValue('')
  await expect(template.locator('option')).toHaveText(['Default', riskTemplate.name])
  await expect(page.getByRole('checkbox')).toHaveCount(0)
  await expect(page.getByLabel('Role', { exact: true })).toHaveValue('')
  for (const removed of ['Personality', 'Work style', 'Boundaries']) {
    await expect(page.getByLabel(removed, { exact: true })).toHaveCount(0)
  }
  await expect(soul).toBeEditable()
  await expect(soul).toHaveValue(defaultSoul)

  await template.selectOption(riskTemplate.id)

  await expect(page.getByLabel('Role', { exact: true })).toHaveValue('Risk Manager')
  await expect(soul).toHaveValue([
    '# Soul - {{agent_name}}', '',
    '## Personality', '- Calm.', '',
    '## Boundaries', '- No trades without approval.', '',
    '## Work Style', '- Stage first.', '',
  ].join('\n'))
  const edited = '# Soul - {{agent_name}}\n\n## Personality\n- Calm and exact.\n'
  await soul.fill(edited)
  await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)
  await expect(soul).toHaveValue(edited.replace('{{agent_name}}', delayedAgent.name))
  await page.getByRole('button', { name: 'Create', exact: true }).click()

  await expect(page).toHaveURL(`/app/agents/${delayedAgent.id}/chat`)
  expect(writes).toEqual([{ method: 'POST', body: {
    name: delayedAgent.name, template_id: riskTemplate.id, role_description: 'Risk Manager', permission_scope_type: 'user',
    permission_access_level: 'use', gogcli_enabled: true, soul: edited.replace('{{agent_name}}', delayedAgent.name),
  } }])
})

test('restores the default soul when the template goes back to Default', async ({ page }) => {
  await prepareOrgDepartments(page)
  await page.route('**/api/agents/templates', (route) => route.fulfill({ json: [riskTemplate] }))
  await page.goto(newAgentPath)
  const soul = page.getByLabel('Soul', { exact: true })
  await page.getByLabel('Template', { exact: true }).selectOption(riskTemplate.id)
  await expect(soul).toHaveValue(/- Calm\./)

  await page.getByLabel('Template', { exact: true }).selectOption('')

  await expect(page.getByLabel('Role', { exact: true })).toHaveValue('')
  await expect(soul).toHaveValue(defaultSoul)
})

test('keeps every name and role mention in the soul in step with the Name and Role fields without touching other edits', async ({ page }) => {
  await prepareOrgDepartments(page)
  await page.route('**/api/agents/default-soul', (route) => route.fulfill({ json: {
    soul: '# Soul - {{agent_name}}\n\n## Identity\n- **Name**: {{agent_name}}\n- **Role**: {{role_description}}\n\n## Personality\nI\'m {{agent_name}}.\n',
  } }))
  const writes = await captureWrites(page)
  await page.goto(newAgentPath)
  const soul = page.getByLabel('Soul', { exact: true })
  const identity = (name: string, role: string, personality = "I'm {{agent_name}}.") =>
    `# Soul - ${name}\n\n## Identity\n- **Name**: ${name}\n- **Role**: ${role}\n\n## Personality\n${personality}\n`
  await expect(soul).toHaveValue(identity('{{agent_name}}', '{{role_description}}'))

  await page.getByLabel('Name', { exact: true }).fill('Rita')
  await page.getByLabel('Role', { exact: true }).fill('Risk\nanalyst')
  await expect(soul).toHaveValue(identity('Rita', 'Risk analyst', "I'm Rita."))

  await soul.fill(identity('Rita', 'Risk analyst', "I'm Rita. Calm and exact."))
  await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)
  await page.getByLabel('Role', { exact: true }).fill('')
  await expect(soul).toHaveValue(identity(delayedAgent.name, '{{role_description}}', `I'm ${delayedAgent.name}. Calm and exact.`))
  await page.getByLabel('Name', { exact: true }).fill('')
  await expect(soul).toHaveValue(identity('{{agent_name}}', '{{role_description}}', "I'm {{agent_name}}. Calm and exact."))
  await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)
  await page.getByLabel('Role', { exact: true }).fill('Risk analyst')
  await page.getByRole('button', { name: 'Create', exact: true }).click()

  await expect(page).toHaveURL(`/app/agents/${delayedAgent.id}/chat`)
  expect(writes).toEqual([{ method: 'POST', body: {
    name: delayedAgent.name, role_description: 'Risk analyst', permission_scope_type: 'user',
    permission_access_level: 'use', gogcli_enabled: true, soul: identity(delayedAgent.name, 'Risk analyst', `I'm ${delayedAgent.name}. Calm and exact.`),
  } }])
})

test('fills the creator and creation date into the soul', async ({ page }) => {
  await prepareOrgDepartments(page)
  await page.route('**/api/agents/default-soul', (route) => route.fulfill({ json: {
    soul: '# Soul - {{agent_name}}\n\n## Identity\n- **Created by**: {{creator_name}}\n- **Created at**: {{created_at}}\n',
  } }))
  await page.clock.setFixedTime(new Date('2030-01-02T12:00:00'))

  await page.goto(newAgentPath)

  await expect(page.getByLabel('Soul', { exact: true })).toHaveValue(
    '# Soul - {{agent_name}}\n\n## Identity\n- **Created by**: Original fencing member\n- **Created at**: 2030-01-02\n',
  )
})

for (const visibility of ['default private', 'private', 'company'] as const) {
  test(`creates with ${visibility} scope and omits previously selected department grants`, async ({ page }) => {
    const state = await prepareOrgDepartments(page)
    const writes = await captureWrites(page)
    await page.goto(newAgentPath)
    await expect(page.getByLabel('Visibility')).toHaveValue('private')
    expect(state.optionsRequests).toBe(0)
    await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)
    if (visibility !== 'default private') {
      await page.getByLabel('Visibility').selectOption('department')
      await page.getByLabel('Department (required)').selectOption(orgDepartment.id)
      await page.getByLabel('Visibility').selectOption(visibility)
    }

    await page.getByRole('button', { name: 'Create', exact: true }).click()

    await expect(page).toHaveURL(`/app/agents/${delayedAgent.id}/chat`)
    expect(writes).toEqual([{ method: 'POST', body: {
      name: delayedAgent.name, role_description: '', permission_scope_type: visibility === 'company' ? 'company' : 'user',
      permission_access_level: 'use', gogcli_enabled: true, soul: soulFor(delayedAgent.name),
    } }])
    expect(state.privilegedRequests).toBe(0)
  })
}

test('creates one department grant atomically and prevents pending duplicate submissions', async ({ page }) => {
  const state = await prepareOrgDepartments(page)
  const release = Promise.withResolvers<void>()
  const writes = await captureWrites(page, 200, release.promise)
  await chooseDepartment(page)

  await page.getByRole('button', { name: 'Create', exact: true }).click()

  const pending = page.getByRole('button', { name: 'Creating...', exact: true })
  await expect(pending).toBeDisabled()
  await expect(pending).toHaveAttribute('aria-busy', 'true')
  await page.locator('form').dispatchEvent('submit')
  release.resolve()
  await expect(page).toHaveURL(`/app/agents/${delayedAgent.id}/chat`)
  await expect(page.locator('[data-sonner-toast][data-type="success"]')).toHaveCount(1)
  expect(writes).toEqual([{ method: 'POST', body: {
    name: delayedAgent.name, role_description: '', permission_scope_type: 'custom',
    department_ids: [orgDepartment.id], permission_access_level: 'use', gogcli_enabled: true, soul: soulFor(delayedAgent.name),
  } }])
  expect(state.privilegedRequests).toBe(0)
})

test('focuses and describes the required department without sending a creation request', async ({ page }) => {
  await prepareOrgDepartments(page)
  const writes = await captureWrites(page)
  await page.goto(newAgentPath)
  await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)
  await page.getByLabel('Visibility').selectOption('department')
  const department = page.getByLabel('Department (required)')
  await expect(department).toBeEnabled()

  await expect(page.getByRole('button', { name: 'Create', exact: true })).toBeDisabled()
  await page.locator('form').dispatchEvent('submit')

  await expect(department).toBeFocused()
  await expect(department).toHaveAttribute('aria-invalid', 'true')
  await expect(department).toHaveAttribute('aria-describedby', /department-error/)
  await expect(page.locator('#department-error')).toBeVisible()
  expect(writes).toEqual([])
})

test('keeps private and company creation available while department options load', async ({ page }) => {
  await prepareOrgDepartments(page)
  const writes = await captureWrites(page)
  const release = Promise.withResolvers<void>()
  await page.route('**/api/departments/options', async (route) => {
    await release.promise
    await route.fulfill({ json: { departments: [orgDepartment] } })
  })
  await page.goto(newAgentPath)
  await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)
  await page.getByLabel('Visibility').selectOption('department')

  await expect(page.locator('#department-status')).toContainText('Loading org departments')
  await expect(page.getByLabel('Department (required)')).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Create', exact: true })).toBeDisabled()
  await page.locator('form').dispatchEvent('submit')
  expect(writes).toEqual([])
  for (const visibility of ['private', 'company']) {
    await page.getByLabel('Visibility').selectOption(visibility)
    await expect(page.getByRole('button', { name: 'Create', exact: true })).toBeEnabled()
  }
  await page.getByLabel('Visibility').selectOption('department')
  release.resolve()
  await expect(page.getByLabel('Department (required)')).toBeEnabled()
})

test('offers an explicit retry after option errors and recovers the selector', async ({ page }) => {
  await prepareOrgDepartments(page)
  let recovered = false
  await page.route('**/api/departments/options', (route) => route.fulfill(recovered
    ? { json: { departments: [orgDepartment] } }
    : { status: 503, json: { detail: 'Options temporarily unavailable' } }))
  await page.goto(newAgentPath)
  await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)
  await page.getByLabel('Visibility').selectOption('department')
  const retry = page.getByRole('button', { name: 'Retry departments' })
  await expect(retry).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('button', { name: 'Create', exact: true })).toBeDisabled()
  await page.getByLabel('Visibility').selectOption('private')
  await expect(page.getByRole('button', { name: 'Create', exact: true })).toBeEnabled()
  await page.getByLabel('Visibility').selectOption('department')
  await expect(retry).toBeEnabled({ timeout: 15_000 })
  recovered = true

  await retry.click()

  await expect(page.getByLabel('Department (required)')).toBeEnabled()
  await expect(retry).toHaveCount(0)
})

test('cleans up a successful department retry that never received focus without stealing focus', async ({ page }) => {
  await prepareOrgDepartments(page)
  let retryStarted = false
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/departments/options', async (route) => {
    if (!retryStarted) return route.fulfill({ status: 503, json: { detail: 'Options temporarily unavailable' } })
    requested.resolve()
    await release.promise
    await route.fulfill({ json: { departments: [orgDepartment] } })
  })
  await page.goto(newAgentPath)
  await page.getByLabel('Visibility').selectOption('department')
  const retry = page.getByRole('button', { name: 'Retry departments' })
  await expect(retry).toBeVisible({ timeout: 15_000 })
  const name = page.getByLabel('Name', { exact: true })
  await name.focus()
  retryStarted = true

  await retry.evaluate((button) => button.click())

  await requested.promise
  await expect(retry).toHaveAttribute('aria-busy', 'true')
  await expect(name).toBeFocused()
  release.resolve()
  await expect(page.getByLabel('Department (required)')).toBeEnabled()
  await expect(retry).toHaveCount(0)
  await expect(name).toBeFocused()
})

for (const outcome of ['success', 'repeated failure', 'focus moved', 'visibility changed', 'session retired'] as const) {
  test(`keyboard department retry preserves focus ownership on ${outcome}`, async ({ page }) => {
    await prepareOrgDepartments(page)
    let retryStarted = false
    let retryRequests = 0
    let release = Promise.withResolvers<void>()
    await page.route('**/api/departments/options', async (route) => {
      if (retryStarted) {
        retryRequests++
        await release.promise
      }
      await route.fulfill(retryStarted && outcome !== 'repeated failure'
        ? { json: { departments: [orgDepartment] } }
        : { status: 503, json: { detail: 'Options temporarily unavailable' } })
    })
    await page.goto(newAgentPath)
    await page.getByLabel('Visibility').selectOption('department')
    const retry = page.getByRole('button', { name: 'Retry departments' })
    await expect(retry).toBeVisible({ timeout: 15_000 })
    retryStarted = true
    for (let attempt = 0; attempt < (outcome === 'repeated failure' ? 2 : 1); attempt++) {
      release = Promise.withResolvers<void>()
      const requestsBefore = retryRequests
      await retry.focus()

      await page.keyboard.press('Enter')

      await expect.poll(() => retryRequests).toBe(requestsBefore + 1)
      await expect(retry).toBeFocused()
      await expect(retry).toHaveAttribute('aria-busy', 'true')
      await expect(retry).toBeFocused()
      await page.keyboard.press('Enter')
      if (outcome === 'focus moved') await page.getByLabel('Name', { exact: true }).focus()
      if (outcome === 'visibility changed') {
        await page.getByLabel('Visibility').selectOption('company')
        await page.getByLabel('Visibility').focus()
      }
      if (outcome === 'session retired') await page.getByRole('button', { name: 'Sign out', exact: true }).first().click()
      expect(retryRequests).toBe(requestsBefore + 1)
      release.resolve()

      switch (outcome) {
        case 'success': await expect(page.getByLabel('Department (required)')).toBeFocused(); break
        case 'repeated failure':
          await expect(retry).toHaveAttribute('aria-busy', 'false', { timeout: 15_000 })
          await expect(retry).toBeFocused()
          break
        case 'focus moved':
          await expect(page.getByLabel('Department (required)')).toBeEnabled()
          await expect(page.getByLabel('Name', { exact: true })).toBeFocused()
          break
        case 'visibility changed':
          await expect(page.getByLabel('Department (required)')).toHaveCount(0)
          await expect(page.getByLabel('Visibility')).toBeFocused()
          break
        case 'session retired': await expect(page).toHaveURL('/login'); break
        default: outcome satisfies never
      }
    }
  })
}

test('explains empty local departments and leaves company visibility usable', async ({ page }) => {
  const state = await prepareOrgDepartments(page)
  state.departments = []
  await page.goto(newAgentPath)
  await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)

  await page.getByLabel('Visibility').selectOption('department')

  await expect(page.locator('#department-status')).toContainText('No org departments yet')
  await expect(page.locator('#department-admin-hint')).toBeVisible()
  await expect(page.getByLabel('Department (required)')).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Create', exact: true })).toBeDisabled()
  await page.getByLabel('Visibility').selectOption('company')
  await expect(page.getByRole('button', { name: 'Create', exact: true })).toBeEnabled()
  expect(state.privilegedRequests).toBe(0)
})

test('preserves the department form and reports a rejected creation', async ({ page }) => {
  await prepareOrgDepartments(page)
  const writes = await captureWrites(page, 422)
  await chooseDepartment(page)

  await page.getByRole('button', { name: 'Create', exact: true }).click()

  await expect(page.locator('[data-sonner-toast][data-type="error"]')).toContainText('Selected department is no longer available')
  await expect(page).toHaveURL(newAgentPath)
  await expect(page.getByLabel('Name', { exact: true })).toHaveValue(delayedAgent.name)
  await expect(page.getByLabel('Department (required)')).toHaveValue(orgDepartment.id)
  await expect(page.getByRole('button', { name: 'Create', exact: true })).toBeEnabled()
  expect(writes.map((write) => write.method)).toEqual(['POST'])
})

for (const boundary of ['storage replacement', 'sign-out'] as const) {
  for (const outcome of ['success', 'error'] as const) {
    test(`retired department creation ${outcome} has no effects after ${boundary}`, async ({ page }) => {
      await prepareOrgDepartments(page)
      const creation = await deferCreation(page, outcome)
      await chooseDepartment(page)
      await page.getByRole('button', { name: 'Create', exact: true }).click()
      await creation.requested
      switch (boundary) {
        case 'storage replacement':
          await writeFromOtherTab(page, replacementToken)
          await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
          await expect(page.getByText(`Signed in as ${replacementUser.display_name}.`, { exact: false })).toBeVisible()
          break
        case 'sign-out':
          await page.getByRole('button', { name: 'Sign out', exact: true }).first().click()
          await expect(page).toHaveURL('/login')
          break
        default: boundary satisfies never
      }
      await page.clock.install({ time: new Date('2030-01-01T00:00:00Z') })
      await page.clock.pauseAt(new Date('2030-01-01T01:00:00Z'))
      const ownerUrl = page.url()
      const completed = page.waitForEvent('requestfinished', {
        predicate: (request) => request.method() === 'POST' && new URL(request.url()).pathname === '/api/agents/',
      })

      creation.release()

      await completed
      await page.clock.runFor(1_000)
      await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
      await expect(page).toHaveURL(ownerUrl)
      expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(boundary === 'sign-out' ? null : replacementToken)
    })
  }
}
