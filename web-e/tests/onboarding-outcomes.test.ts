import assert from 'node:assert/strict'
import { test } from 'node:test'
import { MutationObserver, QueryClient } from '@tanstack/react-query'
import { assistantOnboardingOptions } from '../src/hooks/onboarding-options.ts'
import { createSessionQueryScope, fenceMutationOptions, RetiredWorkspaceError } from '../src/hooks/session-query-scope.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import { getOnboardingStatus, type OnboardingStatus } from '../src/lib/onboarding-api.ts'
import { listAgents, type AgentOut } from '../src/lib/workspace-api.ts'
import { ownAccountSession } from './auth-account-fixture.ts'

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
const startFailure = { status: 'failed', code: 'guest_start_failed', message: 'The guest could not start.' } as const

test('a persisted assistant with a failed start stays on onboarding without completing or announcing ready', async (context) => {
  // Given
  const { owner } = ownAccountSession(context, session)
  const client = new QueryClient()
  context.after(() => client.clear())
  const scope = createSessionQueryScope(authSession, owner, client)
  const paths: string[] = []
  const ready: string[] = []
  const failures: unknown[] = []
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    paths.push(path)
    return Response.json(path.endsWith('/personal-assistant') ? { agent: assistant, onboarding, start: startFailure } : onboarding)
  })
  const mutation = new MutationObserver(client, fenceMutationOptions(scope, assistantOnboardingOptions(scope, {
    ready: (agent) => { ready.push(agent.id) }, startFailed: (failure) => { failures.push(failure) }, error: () => {},
  })))
  // When
  await mutation.mutate({ name: assistant.name })
  // Then
  assert.deepEqual(paths, ['/api/onboarding/start', '/api/onboarding/personal-assistant'])
  assert.deepEqual(ready, [])
  assert.deepEqual(failures, [startFailure])
})

test('quota rejection remains an HTTP error without completing onboarding or announcing ready', async (context) => {
  // Given
  const { owner } = ownAccountSession(context, session)
  const client = new QueryClient()
  context.after(() => client.clear())
  const paths: string[] = []
  let ready = 0
  let failures = 0
  const errors: Error[] = []
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    paths.push(path)
    return path.endsWith('/personal-assistant')
      ? Response.json({ detail: { code: 'agent_quota_exceeded', message: 'Agent quota reached.' } }, { status: 403 })
      : Response.json(onboarding)
  })
  const scope = createSessionQueryScope(authSession, owner, client)
  const mutation = new MutationObserver(client, fenceMutationOptions(scope, assistantOnboardingOptions(scope, {
    ready: () => { ready++ }, startFailed: () => { failures++ }, error: (error) => { errors.push(error) },
  })))
  // When
  await assert.rejects(mutation.mutate({ name: assistant.name }), (error) => error instanceof ApiError && error.status === 403)
  // Then
  assert.deepEqual(paths, ['/api/onboarding/start', '/api/onboarding/personal-assistant'])
  assert.equal(ready, 0)
  assert.equal(failures, 0)
  assert.equal(errors.length, 1)
})

test('a started assistant completes onboarding before the ready effect opens its chat', async (context) => {
  // Given
  const { owner } = ownAccountSession(context, session)
  const client = new QueryClient()
  context.after(() => client.clear())
  const scope = createSessionQueryScope(authSession, owner, client)
  const events: string[] = []
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    events.push(path)
    return Response.json(path.endsWith('/personal-assistant')
      ? { agent: assistant, onboarding, api_key: 'existing-optional-key', start: { status: 'started' } }
      : path.endsWith('/complete') ? { ...onboarding, status: 'completed', current_step: 'completed' } : onboarding)
  })
  const mutation = new MutationObserver(client, fenceMutationOptions(scope, assistantOnboardingOptions(scope, {
    ready: (agent) => { events.push(`chat:${agent.id}`) },
    startFailed: () => assert.fail('A started assistant must not be reported as failed'),
    error: () => assert.fail('A started assistant must not be reported as an HTTP error'),
  })))
  // When
  const result = await mutation.mutate({ name: assistant.name, personality: 'warm' })
  // Then
  assert.deepEqual(events, ['/api/onboarding/start', '/api/onboarding/personal-assistant', '/api/onboarding/complete', `chat:${assistant.id}`])
  assert.equal(result.api_key, 'existing-optional-key')
  assert.equal(result.onboarding.personal_assistant_agent_id, assistant.id)
  assert.equal(result.onboarding.status, 'completed')
})

test('retry refreshes fresh gate caches before opening the persisted assistant chat', async (context) => {
  // Given a persisted assistant whose first start failed
  const { owner } = ownAccountSession(context, session)
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, retry: false } } })
  context.after(() => client.clear())
  const scope = createSessionQueryScope(authSession, owner, client)
  const bodies: unknown[] = []
  const ready: { agentId: string; gateAgents: string[]; onboardingStatus: string | undefined }[] = []
  let completions = 0
  context.mock.method(globalThis, 'fetch', async (path: string, options?: RequestInit) => {
    if (path === '/api/agents/') return Response.json(completions ? [{ ...assistant, role_description: '', status: 'running', creator_id: session.user.id }] : [])
    if (path.endsWith('/personal-assistant')) {
      bodies.push(JSON.parse(String(options?.body)))
      return Response.json({ agent: assistant, onboarding, start: bodies.length === 1 ? startFailure : { status: 'started' } })
    }
    if (path.endsWith('/complete')) completions++
    return Response.json(completions ? { ...onboarding, status: 'completed', current_step: 'completed' } : onboarding)
  })
  await client.fetchQuery({ queryKey: ['agents'], queryFn: listAgents })
  await client.fetchQuery({ queryKey: ['onboarding'], queryFn: getOnboardingStatus })
  const mutation = new MutationObserver(client, fenceMutationOptions(scope, assistantOnboardingOptions(scope, {
    ready: (agent) => {
      ready.push({
        agentId: agent.id,
        gateAgents: client.getQueryData<AgentOut[]>(['agents'])?.map((entry) => entry.id) ?? [],
        onboardingStatus: client.getQueryData<OnboardingStatus>(['onboarding'])?.status,
      })
    },
    startFailed: () => {}, error: () => {},
  })))
  const input = { name: assistant.name, personality: 'warm', work_style: 'concise', boundaries: '' }
  await mutation.mutate(input)
  assert.deepEqual(ready, [])
  assert.equal(completions, 0)
  assert.deepEqual(client.getQueryData(['agents']), [])
  assert.equal(client.getQueryData<OnboardingStatus>(['onboarding'])?.status, 'in_progress')
  // When
  await mutation.mutate(input)
  // Then
  assert.deepEqual(bodies, [input, input])
  assert.equal(completions, 1)
  assert.deepEqual(ready, [{ agentId: assistant.id, gateAgents: [assistant.id], onboardingStatus: 'completed' }])
})

test('completion failure does not announce a started assistant as ready', async (context) => {
  // Given
  const { owner } = ownAccountSession(context, session)
  const client = new QueryClient()
  context.after(() => client.clear())
  const scope = createSessionQueryScope(authSession, owner, client)
  let ready = 0
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    if (path.endsWith('/complete')) return Response.json({ detail: 'Unavailable' }, { status: 503 })
    return Response.json(path.endsWith('/personal-assistant') ? { agent: assistant, onboarding, start: { status: 'started' } } : onboarding)
  })
  const mutation = new MutationObserver(client, fenceMutationOptions(scope, assistantOnboardingOptions(scope, {
    ready: () => { ready++ }, startFailed: () => {}, error: () => {},
  })))
  // When
  await assert.rejects(mutation.mutate({ name: assistant.name }), (error) => error instanceof ApiError && error.status === 503)
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
    context.mock.method(globalThis, 'fetch', async (path: string) => {
      if (path === '/api/agents/') {
        if (agentReads++ === 0) return Response.json([])
        entered.resolve()
        return response.promise
      }
      return Response.json(path.endsWith('/personal-assistant')
        ? { agent: assistant, onboarding, start: { status: 'started' } }
        : { ...onboarding, status: 'completed', current_step: 'completed' })
    })
    await client.fetchQuery({ queryKey: ['agents'], queryFn: listAgents })
    const mutation = new MutationObserver(client, fenceMutationOptions(scope, assistantOnboardingOptions(scope, {
      ready: () => { ready++ }, startFailed: () => {}, error: () => {},
    })))
    const pending = mutation.mutate({ name: assistant.name })
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

for (const phase of ['start', 'personal-assistant', 'complete']) {
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
        if (path === `/api/onboarding/${phase}`) { entered.resolve(); return response.promise }
        return Response.json(path.endsWith('/personal-assistant') ? { agent: assistant, onboarding, start: { status: 'started' } } : onboarding)
      })
      const mutation = new MutationObserver(client, fenceMutationOptions(scope, assistantOnboardingOptions(scope, {
        ready: () => { effects++ }, startFailed: () => { effects++ }, error: () => { effects++ },
      })))
      const pending = mutation.mutate({ name: assistant.name })
      await entered.promise
      // When
      if (transition === 'logout') authSession.logout(owner)
      else authSession.applySession(owner, { ...session, access_token: 'other-workspace-token' })
      const retained = authSession.getSnapshot()
      response.resolve(Response.json(phase === 'personal-assistant' ? { agent: assistant, onboarding, start: { status: 'started' } } : onboarding))
      await assert.rejects(pending, StaleSessionError)
      // Then
      assert.equal(paths.at(-1), `/api/onboarding/${phase}`)
      assert.equal(effects, 0)
      assert.equal(authSession.getSnapshot(), retained)
    })
  }
}
