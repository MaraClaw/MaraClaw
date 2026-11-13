import assert from 'node:assert/strict'
import { test } from 'node:test'
import { MutationObserver, QueryClient } from '@tanstack/react-query'
import { assistantOnboardingOptions } from '../src/hooks/onboarding-options.ts'
import { createSessionQueryScope, fenceMutationOptions, RetiredWorkspaceError } from '../src/hooks/session-query-scope.ts'
import { authSession } from '../src/lib/auth-session.ts'
import type { AgentCreate } from '../src/lib/engine-contracts.generated.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import { getOnboardingStatus, type OnboardingStatus } from '../src/lib/onboarding-api.ts'
import { listAgents, type AgentOut } from '../src/lib/workspace-api.ts'
import { ownAccountSession } from './auth-account-fixture.ts'
import { agentExistenceOptions, agentPageOptions } from '../src/lib/agents-query.ts'
import { listAgentsPage } from '../src/lib/agents-api.ts'

const session = {
  access_token: 'onboarding-session',
  user: { id: '11111111-1111-4111-8111-111111111111', email: 'member@example.test', display_name: 'Member', role: 'member', tenant_id: 'company', email_verified: true },
} as const
const onboarding = { exists: true, status: 'in_progress', current_step: 'personal_assistant', entry_mode: 'join', personal_assistant_agent_id: '22222222-2222-4222-8222-222222222222', completed_at: null }
const assistant = {
  id: '22222222-2222-4222-8222-222222222222', name: 'My assistant', role_description: 'Assistant',
  status: 'stopped', creator_id: session.user.id, autonomy_policy: {},
  tokens_used_today: 0, tokens_used_month: 0, created_at: '2026-10-25T08:00:00Z',
}
const input: AgentCreate = {
  name: assistant.name, role_description: '', permission_scope_type: 'user', permission_access_level: 'use',
  gogcli_enabled: false, personality: 'Personality: warm. Work style: concise.', boundaries: 'No purchases.',
}
const phasePaths = { start: '/api/onboarding/start', agents: '/api/agents/', complete: '/api/onboarding/complete' } as const

test('quota rejection remains an HTTP error without completing onboarding or announcing ready', async (context) => {
  // Given
  const { owner } = ownAccountSession(context, session)
  const client = new QueryClient()
  context.after(() => client.clear())
  const paths: string[] = []
  let ready = 0
  const errors: Error[] = []
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    paths.push(path)
    return path === '/api/agents/'
      ? Response.json({ detail: { code: 'agent_quota_exceeded', message: 'Agent quota reached.' } }, { status: 403 })
      : Response.json(onboarding)
  })
  const scope = createSessionQueryScope(authSession, owner, client)
  const mutation = new MutationObserver(client, fenceMutationOptions(scope, assistantOnboardingOptions(scope, {
    ready: () => { ready++ }, error: (error) => { errors.push(error) },
  })))
  // When
  await assert.rejects(mutation.mutate(input), (error) => error instanceof ApiError && error.status === 403)
  // Then
  assert.deepEqual(paths, ['/api/onboarding/start', '/api/agents/'])
  assert.equal(ready, 0)
  assert.equal(errors.length, 1)
})

test('a created assistant completes onboarding before the ready effect opens its chat', async (context) => {
  // Given
  const { owner } = ownAccountSession(context, session)
  const client = new QueryClient()
  context.after(() => client.clear())
  const scope = createSessionQueryScope(authSession, owner, client)
  const events: string[] = []
  const created: { method: string | undefined; body: unknown }[] = []
  context.mock.method(globalThis, 'fetch', async (path: string, options?: RequestInit) => {
    events.push(path)
    if (path === '/api/agents/') {
      created.push({ method: options?.method, body: JSON.parse(String(options?.body)) })
      return Response.json(assistant)
    }
    return Response.json(path.endsWith('/complete') ? { ...onboarding, status: 'completed', current_step: 'completed' } : onboarding)
  })
  const mutation = new MutationObserver(client, fenceMutationOptions(scope, assistantOnboardingOptions(scope, {
    ready: (agent) => { events.push(`chat:${agent.id}`) },
    error: () => assert.fail('A created assistant must not be reported as an HTTP error'),
  })))
  // When
  const result = await mutation.mutate(input)
  // Then
  assert.deepEqual(events, ['/api/onboarding/start', '/api/agents/', '/api/onboarding/complete', `chat:${assistant.id}`])
  assert.deepEqual(created, [{ method: 'POST', body: input }])
  assert.equal(result.agent.id, assistant.id)
  assert.equal(result.onboarding.personal_assistant_agent_id, assistant.id)
  assert.equal(result.onboarding.status, 'completed')
})

test('completion refreshes fresh gate caches before opening the created assistant chat', async (context) => {
  // Given gate caches that still show no agent and an unfinished onboarding
  const { owner } = ownAccountSession(context, session)
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, retry: false } } })
  context.after(() => client.clear())
  const scope = createSessionQueryScope(authSession, owner, client)
  const ready: { agentId: string; gateAgents: string[]; onboardingStatus: string | undefined }[] = []
  let completions = 0
  context.mock.method(globalThis, 'fetch', async (path: string, options?: RequestInit) => {
    if (path === '/api/agents/') {
      if (options?.method === 'POST') return Response.json(assistant)
      return Response.json(completions ? [{ ...assistant, role_description: '', status: 'running', creator_id: session.user.id }] : [])
    }
    if (path.startsWith('/api/agents/page?')) return Response.json({ items: completions ? [assistant] : [], next_cursor: null, has_more: false })
    if (path.endsWith('/complete')) completions++
    return Response.json(completions ? { ...onboarding, status: 'completed', current_step: 'completed' } : onboarding)
  })
  await client.fetchQuery({ queryKey: ['agents'], queryFn: listAgents })
  await client.fetchQuery(agentExistenceOptions())
  await client.fetchInfiniteQuery(agentPageOptions())
  await client.fetchQuery({ queryKey: ['onboarding'], queryFn: getOnboardingStatus })
  const mutation = new MutationObserver(client, fenceMutationOptions(scope, assistantOnboardingOptions(scope, {
    ready: (agent) => {
      ready.push({
        agentId: agent.id,
        gateAgents: client.getQueryData<AgentOut[]>(['agents'])?.map((entry) => entry.id) ?? [],
        onboardingStatus: client.getQueryData<OnboardingStatus>(['onboarding'])?.status,
      })
    },
    error: () => {},
  })))
  assert.deepEqual(client.getQueryData(['agents']), [])
  assert.equal(client.getQueryData<OnboardingStatus>(['onboarding'])?.status, 'in_progress')
  // When
  await mutation.mutate(input)
  // Then
  assert.equal(completions, 1)
  assert.deepEqual(ready, [{ agentId: assistant.id, gateAgents: [assistant.id], onboardingStatus: 'completed' }])
  assert.deepEqual(client.getQueryData<Awaited<ReturnType<typeof listAgentsPage>>>(agentExistenceOptions().queryKey)?.items.map((entry) => entry.id), [assistant.id])
  assert.deepEqual(client.getQueryData<{ pages: Awaited<ReturnType<typeof listAgentsPage>>[] }>(agentPageOptions().queryKey)?.pages[0]?.items.map((entry) => entry.id), [assistant.id])
})

test('completion failure does not announce a created assistant as ready', async (context) => {
  // Given
  const { owner } = ownAccountSession(context, session)
  const client = new QueryClient()
  context.after(() => client.clear())
  const scope = createSessionQueryScope(authSession, owner, client)
  let ready = 0
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    if (path.endsWith('/complete')) return Response.json({ detail: 'Unavailable' }, { status: 503 })
    return Response.json(path === '/api/agents/' ? assistant : onboarding)
  })
  const mutation = new MutationObserver(client, fenceMutationOptions(scope, assistantOnboardingOptions(scope, {
    ready: () => { ready++ }, error: () => {},
  })))
  // When
  await assert.rejects(mutation.mutate(input), (error) => error instanceof ApiError && error.status === 503)
  // Then
  assert.equal(ready, 0)
})

for (const transition of ['logout', 'replacement']) {
  test(`retiring the owner during the gate refresh prevents chat navigation after ${transition}`, async (context) => {
    // Given
    const { owner } = ownAccountSession(context, session)
    const client = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, retry: false } } })
    context.after(() => client.clear())
    const scope = createSessionQueryScope(authSession, owner, client)
    context.after(scope.subscribe())
    const entered = Promise.withResolvers<void>()
    const response = Promise.withResolvers<Response>()
    let agentReads = 0
    let ready = 0
    context.mock.method(globalThis, 'fetch', async (path: string, options?: RequestInit) => {
      if (path === '/api/agents/' && options?.method !== 'POST') {
        if (agentReads++ === 0) return Response.json([])
        entered.resolve()
        return response.promise
      }
      return Response.json(path === '/api/agents/'
        ? assistant
        : { ...onboarding, status: 'completed', current_step: 'completed' })
    })
    await client.fetchQuery({ queryKey: ['agents'], queryFn: listAgents })
    const mutation = new MutationObserver(client, fenceMutationOptions(scope, assistantOnboardingOptions(scope, {
      ready: () => { ready++ }, error: () => {},
    })))
    const pending = mutation.mutate(input)
    await entered.promise
    // When
    if (transition === 'logout') authSession.logout(owner)
    else authSession.applySession(owner, { ...session, access_token: 'replacement-workspace' })
    response.resolve(Response.json([assistant]))
    await assert.rejects(pending, RetiredWorkspaceError)
    // Then
    assert.equal(ready, 0)
    assert.deepEqual(client.getQueryCache().getAll(), [])
  })
}

for (const phase of ['start', 'agents', 'complete'] as const) {
  for (const transition of ['logout', 'replacement']) {
    test(`a late ${phase} response after ${transition} cannot advance onboarding or deliver page effects`, async (context) => {
      // Given
      const { owner } = ownAccountSession(context, session)
      const client = new QueryClient()
      context.after(() => client.clear())
      const scope = createSessionQueryScope(authSession, owner, client)
      const entered = Promise.withResolvers<void>()
      const response = Promise.withResolvers<Response>()
      const paths: string[] = []
      let effects = 0
      context.mock.method(globalThis, 'fetch', async (path: string) => {
        paths.push(path)
        if (path === phasePaths[phase]) { entered.resolve(); return response.promise }
        return Response.json(path === '/api/agents/' ? assistant : onboarding)
      })
      const mutation = new MutationObserver(client, fenceMutationOptions(scope, assistantOnboardingOptions(scope, {
        ready: () => { effects++ }, error: () => { effects++ },
      })))
      const pending = mutation.mutate(input)
      await entered.promise
      // When
      if (transition === 'logout') authSession.logout(owner)
      else authSession.applySession(owner, { ...session, access_token: 'other-workspace-token' })
      const retained = authSession.getSnapshot()
      response.resolve(Response.json(phase === 'agents' ? assistant : onboarding))
      await assert.rejects(pending, StaleSessionError)
      // Then
      assert.equal(paths.at(-1), phasePaths[phase])
      assert.equal(effects, 0)
      assert.equal(authSession.getSnapshot(), retained)
    })
  }
}
