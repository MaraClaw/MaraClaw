import { LRUCache } from 'lru-cache'
import { authSession, type AuthSessionStore, type SessionOwner } from './auth-session.ts'
import { ApiError, BinaryLimitError, MAX_PREVIEW_BYTES, StaleSessionError } from './http.ts'

export type AttachmentResource = { readonly agentId: string; readonly path: string }
type OwnedResource = AttachmentResource & { readonly owner: SessionOwner }
export type AttachmentVersion = OwnedResource & { readonly version: string }
export const ATTACHMENT_CACHE_BYTES = 64 * 1024 * 1024

/** Only this LRU owns retained binary data. Owner identity is an object, never a token. */
export function createAttachmentCache(store: Pick<AuthSessionStore, 'isCurrent' | 'subscribe'>) {
  const bytes = new LRUCache<AttachmentVersion, Uint8Array>({
    max: 256, maxSize: ATTACHMENT_CACHE_BYTES, maxEntrySize: MAX_PREVIEW_BYTES,
    sizeCalculation: (value) => Math.max(1, value.byteLength),
  })
  const versions = new LRUCache<OwnedResource, AttachmentVersion>({ max: 256 })
  const assertCurrent = (owner: SessionOwner) => {
    if (!store.isCurrent(owner)) throw new StaleSessionError(owner)
  }
  const matches = (key: OwnedResource, owner: SessionOwner, resource: AttachmentResource) =>
    key.owner === owner && key.agentId === resource.agentId && key.path === resource.path
  function evict(owner: SessionOwner, resource: AttachmentResource) {
    for (const key of bytes.keys()) if (matches(key, owner, resource)) bytes.delete(key)
    for (const key of versions.keys()) if (matches(key, owner, resource)) versions.delete(key)
  }
  const unsubscribe = store.subscribe(() => {
    for (const key of bytes.keys()) if (!store.isCurrent(key.owner)) bytes.delete(key)
    for (const key of versions.keys()) if (!store.isCurrent(key.owner)) versions.delete(key)
  })
  return {
    authorize(owner: SessionOwner, resource: AttachmentResource, version: string): AttachmentVersion {
      assertCurrent(owner)
      const previous = [...versions.keys()].find((key) => matches(key, owner, resource))
      const current = previous ? versions.get(previous) : undefined
      if (current?.version === version) return current
      evict(owner, resource)
      const key = Object.freeze({ ...resource, owner, version })
      versions.set(key, key)
      return key
    },
    read(key: AttachmentVersion) {
      assertCurrent(key.owner)
      return bytes.get(key)
    },
    put(key: AttachmentVersion, value: Uint8Array) {
      assertCurrent(key.owner)
      if (versions.get(key) !== key) throw new ApiError(409, 'File changed. Open the preview again.')
      if (value.byteLength > MAX_PREVIEW_BYTES) throw new BinaryLimitError()
      bytes.set(key, value)
    },
    evict,
    get size() { return bytes.calculatedSize },
    dispose() { unsubscribe(); bytes.clear(); versions.clear() },
  }
}

export type AttachmentCache = ReturnType<typeof createAttachmentCache>
export const attachmentCache = createAttachmentCache(authSession)
