import assert from 'node:assert/strict'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { expect } from '@playwright/test'

export function installRosterProbe() {
  window.__roster = { created: [], revoked: [], clicks: 0, fail: false }
  const create = URL.createObjectURL.bind(URL)
  const revoke = URL.revokeObjectURL.bind(URL)
  const click = HTMLAnchorElement.prototype.click
  URL.createObjectURL = (blob) => { const url = create(blob); window.__roster.created.push(url); return url }
  URL.revokeObjectURL = (url) => { window.__roster.revoked.push(url); revoke(url) }
  HTMLAnchorElement.prototype.click = function () {
    if (this.download) { window.__roster.clicks++; if (window.__roster.fail) throw new Error('Synthetic download failure') }
    return click.call(this)
  }
  const fetch = window.fetch.bind(window)
  window.fetch = (input, init) => fetch(input, String(input).includes('/api/users/export?') ? { ...init, signal: undefined } : init)
}

const exportButton = (page) => page.getByRole('button', { name: 'Export company CSV', exact: true })
const pendingButton = (page) => page.getByRole('button', { name: 'Exporting company CSV…', exact: true })
const csv = 'id,email,display_name,role,is_active,agents_count,created_at,source\r\nserver-only,off-page@example.test,Off-page member,member,true,2,,local\r\n'
const header = csv.split('\r\n')[0] + '\r\n'
const settle = (page) => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
const probe = (page) => page.evaluate(() => ({ ...window.__roster, anchors: document.querySelectorAll('a[download]').length }))

async function download(page, state) {
  const received = page.waitForEvent('download')
  await exportButton(page).click()
  const file = await received
  await expect(exportButton(page)).toBeEnabled()
  const result = await readFile(await file.path(), 'utf8')
  const resources = await probe(page)
  assert.deepEqual(resources.revoked, resources.created)
  assert.equal(resources.anchors, 0)
  assert.equal(await file.failure(), null)
  assert.equal(state.downloads.length, resources.clicks)
  return { file, result, resources }
}

export async function exerciseRosterExports(scenario, h) {
  const actions = []
  for (const role of ['platform_admin', 'org_admin']) await scenario(`${role} exports complete server CSV for the current company and literal search`, async (page, state) => {
    state.exportAnswer = async () => ({ csv })
    await h.openUsers(page, 'company=beta&q=%20%25_Case%2B%3F%20')
    await expect(exportButton(page)).toBeEnabled()
    const { file, result, resources } = await download(page, state)
    assert.equal(result, csv)
    assert.deepEqual(state.exports, [{ tenant: role === 'platform_admin' ? 'beta' : h.admin.tenant_id, search: ' %_Case+? ' }])
    assert.equal(state.calls.every((call) => call.cursor === null), true)
    assert.equal(result.includes('Hidden supervisor'), false)
    await file.saveAs(join(h.output, `company-roster-${role}.csv`))
    actions.push({ role, request: state.exports[0], download: file.suggestedFilename(), resources })
    await h.ownedBrowser.capture(page, join(h.output, `roster-${role}-download.png`))
  }, { role })

  await scenario('header-only CSV is a successful download with visible outcome', async (page, state) => {
    state.exportAnswer = async () => ({ csv: header })
    state.answer = async () => ({ items: [], next_cursor: null })
    await h.openUsers(page, 'company=alpha&q=missing')
    const { result } = await download(page, state)
    assert.equal(result, header)
    await expect(page.getByRole('status').filter({ hasText: 'column headers only' })).toBeVisible()
    await h.ownedBrowser.capture(page, join(h.output, 'roster-empty-download.png'))
  })

  for (const failure of ['http', 'schema', 'download']) await scenario(`current export ${failure} failure clears pending, cleans URLs and permits retry`, async (page, state) => {
    state.exportAnswer = async () => failure === 'http' ? { status: 503, body: { detail: 'Export temporarily unavailable' } } : failure === 'schema' ? { csv: 4 } : { csv }
    await h.openUsers(page)
    await page.evaluate((fail) => { window.__roster.fail = fail }, failure === 'download')
    await exportButton(page).click()
    await expect(page.getByRole('alert')).toBeVisible()
    await expect(exportButton(page)).toBeEnabled()
    const resources = await probe(page)
    assert.deepEqual(resources.created, resources.revoked)
    assert.equal(resources.anchors, 0)
    await h.ownedBrowser.capture(page, join(h.output, `roster-${failure}-error.png`))
    state.exportAnswer = async () => ({ csv })
    await page.evaluate(() => { window.__roster.fail = false })
    if (failure === 'download') state.downloads.push({ syntheticFailedClick: true })
    const { result } = await download(page, state)
    assert.equal(result, csv)
    await expect(page.getByRole('alert')).toHaveCount(0)
  })

  for (const transition of ['company A-B-A', 'search A-B-A', 'session', 'unmount']) for (const status of [200, 403]) await scenario(`retired export ${status} cannot affect ${transition} or a new pending export`, async (page, state) => {
    const old = h.gated(state)
    const fresh = h.gated(state)
    const started = Promise.withResolvers()
    state.exportAnswer = async () => {
      if (state.exports.length === 1) { started.resolve(); await old.promise; return status === 200 ? { csv } : { status, body: { detail: 'Retired export error' } } }
      await fresh.promise
      return { csv: header }
    }
    await h.openUsers(page)
    await exportButton(page).click()
    await started.promise
    await expect(pendingButton(page)).toBeDisabled()
    await expect(pendingButton(page)).toHaveAttribute('aria-busy', 'true')
    if (transition === 'company A-B-A' && status === 200) await h.ownedBrowser.capture(page, join(h.output, 'roster-pending.png'))
    if (transition === 'company A-B-A') {
      await page.getByRole('combobox').selectOption('beta')
      await expect(h.openUser(page, 'beta member')).toBeVisible()
      await expect(exportButton(page)).toBeEnabled()
      await page.getByRole('combobox').selectOption('alpha')
      await expect(h.openUser(page, 'alpha member')).toBeVisible()
    } else if (transition === 'search A-B-A') {
      await page.getByLabel('Search', { exact: true }).fill('other')
      await expect(h.openUser(page, 'other')).toBeVisible()
      await expect(exportButton(page)).toBeEnabled()
      await page.getByLabel('Search', { exact: true }).fill('')
      await expect(h.openUser(page, 'alpha member')).toBeVisible()
    } else if (transition === 'session') {
      await page.getByRole('button', { name: 'Sign out', exact: true }).click()
      state.role = 'org_admin'
      await page.getByLabel('Email', { exact: true }).fill('operator@example.test')
      await page.getByLabel('Password', { exact: true }).fill('synthetic-password')
      await page.getByRole('button', { name: 'Sign in', exact: true }).click()
      await expect(exportButton(page)).toBeEnabled()
    } else {
      await page.getByRole('link', { name: 'Account', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'Account', exact: true })).toBeVisible()
    }
    const currentView = transition !== 'unmount'
    if (currentView) {
      await expect(exportButton(page)).toBeEnabled()
      await exportButton(page).click()
      await expect.poll(() => state.exports.length).toBe(2)
    }
    old.resolve()
    await expect.poll(() => state.exportReplies).toBe(1)
    await settle(page)
    assert.equal(state.downloads.length, 0)
    assert.deepEqual((await probe(page)).created, [])
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByText('Retired export error', { exact: true })).toHaveCount(0)
    if (currentView) {
      await expect(pendingButton(page)).toBeDisabled()
      const received = page.waitForEvent('download')
      fresh.resolve()
      const file = await received
      assert.equal(await readFile(await file.path(), 'utf8'), header)
      await expect(exportButton(page)).toBeEnabled()
    }
    const resources = await probe(page)
    assert.deepEqual(resources.created, resources.revoked)
    assert.equal(resources.anchors, 0)
    actions.push({ transition, retiredStatus: status, requests: state.exports, downloads: state.downloads.length, resources })
    await h.ownedBrowser.capture(page, join(h.output, `roster-retired-${transition.replaceAll(' ', '-')}-${status}.png`))
  })
  await writeFile(join(h.output, 'roster-action-log.json'), JSON.stringify(actions, null, 2))
}
