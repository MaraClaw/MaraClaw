import { QueryClient } from '@tanstack/react-query'
import { createSessionQueryScope } from '../src/hooks/session-query-scope.ts'
import { createAttachmentCache } from '../src/lib/attachment-cache.ts'
import { createAttachmentPreviewClient } from '../src/lib/attachment-preview-api.ts'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { createHttpClient } from '../src/lib/http.ts'
import type { UserOut } from '../src/lib/types/auth.ts'

export const member: UserOut = {
  id: 'member', email: 'member@example.com', display_name: 'Member',
  role: 'member', tenant_id: 'tenant-a', email_verified: true,
}
export const resource = { agentId: 'agent', path: 'uploads/photo.png' }
export const metadata = {
  path: resource.path, name: 'photo.png', size: 3,
  version_token: 'v1', content_type: 'image/png',
}

export function attachmentFixture(fetchRequest: typeof fetch = () => Promise.resolve(new Response('{}'))) {
  let token: string | null = 'token-a'
  const store = createAuthSessionStore({
    read: () => token, write: (value) => { token = value }, clear: () => { token = null },
  })
  store.applySession(store.capture(), { access_token: 'token-a', user: member })
  const queryClient = new QueryClient()
  const scope = createSessionQueryScope(store, store.capture(), queryClient)
  const unsubscribe = scope.subscribe()
  const cache = createAttachmentCache(store)
  const http = createHttpClient(store, fetchRequest, (path) => `https://api.example${path}`)
  const previews = createAttachmentPreviewClient({
    metadata(resource, { owner, signal }) {
      return http.apiRequest(
        `/api/agents/${encodeURIComponent(resource.agentId)}/files/preview-metadata?${new URLSearchParams({ path: resource.path })}`,
        { owner, signal, cache: 'no-store' },
      )
    },
    bytes(resource, versionToken, { owner, signal }) {
      return http.apiBinaryRequest(`/api/agents/${encodeURIComponent(resource.agentId)}/files/preview-bytes?${new URLSearchParams({
        path: resource.path, version_token: versionToken,
      })}`, { owner, signal })
    },
  }, cache)
  return {
    store, cache, http, scope, queryClient, previews,
    dispose() { unsubscribe(); cache.dispose(); queryClient.clear() },
  }
}
