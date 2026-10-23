import { z } from 'zod'
import type { SessionQueryScope } from '../hooks/session-query-scope.ts'
import { attachmentCache, type AttachmentCache, type AttachmentResource } from './attachment-cache.ts'
import { requestAttachment } from './attachment-request.ts'
import { apiBinaryRequest, apiRequest, ApiError, BinaryLimitError, MAX_PREVIEW_BYTES } from './http.ts'
import { officeFormat } from './office-preview.ts'

const metadataSchema = z.object({
  path: z.string().min(1), name: z.string().min(1), size: z.number().int().nonnegative(),
  version_token: z.string().min(1), content_type: z.string().min(1),
}).readonly()
export type AttachmentMetadata = z.infer<typeof metadataSchema>
export type AttachmentRenderer = 'image' | 'pdf' | 'office' | 'unsupported'
export type AttachmentPreview = { readonly metadata: AttachmentMetadata } & (
  | { readonly renderer: 'image' | 'pdf' | 'office'; readonly bytes: Uint8Array }
  | { readonly renderer: 'unsupported' }
)

export function attachmentRenderer(metadata: AttachmentMetadata): AttachmentRenderer {
  const mime = metadata.content_type.split(';')[0]?.trim().toLowerCase()
  if (mime && ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif', 'image/bmp'].includes(mime)) return 'image'
  if (mime === 'application/pdf') return 'pdf'
  if (officeFormat(metadata.name)) return 'office'
  return 'unsupported'
}

type PreviewContext = {
  readonly owner: SessionQueryScope['owner']
  readonly signal: AbortSignal
}
type PreviewTransport = {
  readonly metadata: (resource: AttachmentResource, context: PreviewContext) => Promise<unknown>
  readonly bytes: (resource: AttachmentResource, versionToken: string, context: PreviewContext) => Promise<Uint8Array>
}

const previewTransport: PreviewTransport = {
  metadata(resource, { owner, signal }) {
    return apiRequest<unknown>(
      `/api/agents/${encodeURIComponent(resource.agentId)}/files/preview-metadata?${new URLSearchParams({ path: resource.path })}`,
      { owner, signal, cache: 'no-store' },
    )
  },
  bytes(resource, versionToken, { owner, signal }) {
    return apiBinaryRequest(`/api/agents/${encodeURIComponent(resource.agentId)}/files/preview-bytes?${new URLSearchParams({
      path: resource.path, version_token: versionToken,
    })}`, { owner, signal })
  },
}

export function createAttachmentPreviewClient(transport: PreviewTransport, cache: AttachmentCache) {
  return {
    async open(scope: SessionQueryScope, resource: AttachmentResource, signal: AbortSignal): Promise<AttachmentPreview> {
      const assertActive = () => { scope.assertCurrent(); signal.throwIfAborted() }
      assertActive()
      try {
        // staleTime=0: every opening reauthorizes before any cached bytes can be returned.
        const metadata = await requestAttachment(scope.client, {
          queryKey: ['attachment-metadata', resource.agentId, resource.path],
          queryFn: async ({ signal }) => metadataSchema.parse(await transport.metadata(resource, { owner: scope.owner, signal })),
        }, signal)
        assertActive()
        const key = cache.authorize(scope.owner, resource, metadata.version_token)
        if (metadata.size > MAX_PREVIEW_BYTES) throw new BinaryLimitError()
        const renderer = attachmentRenderer(metadata)
        if (renderer === 'unsupported') return { metadata, renderer }
        if (!cache.read(key)) {
          await requestAttachment(scope.client, {
            queryKey: ['attachment-bytes', resource.agentId, resource.path, metadata.version_token],
            queryFn: async ({ signal }) => {
              const value = await transport.bytes(resource, metadata.version_token, { owner: scope.owner, signal })
              scope.assertCurrent()
              signal.throwIfAborted()
              cache.put(key, value)
              // Query retains no binary data, including after a failed or cancelled view.
              return metadata.version_token
            },
          }, signal)
        }
        assertActive()
        const bytes = cache.read(key)
        if (!bytes) throw new ApiError(409, 'Preview was evicted. Open the preview again.')
        return { metadata, renderer, bytes }
      } catch (error) {
        if (error instanceof ApiError && [403, 404, 409, 413].includes(error.status)) cache.evict(scope.owner, resource)
        throw error
      }
    },
  }
}

export const attachmentPreviewClient = createAttachmentPreviewClient(previewTransport, attachmentCache)
