import { z } from 'zod'

import { apiRequest, type RequestOptions } from './http.ts'

const byteLimit = z.number().int().positive()
export const uploadPolicySchema = z.object({
  chat_image_max_bytes: byteLimit,
  chat_file_max_bytes: byteLimit,
  workspace_file_max_bytes: byteLimit,
  image_extensions: z.array(z.string().regex(/^\.[a-z0-9]+$/)).min(1).readonly(),
}).readonly()

export type UploadPolicy = z.infer<typeof uploadPolicySchema>

export function getUploadPolicy(options: Pick<RequestOptions, 'owner' | 'signal'> = {}): Promise<UploadPolicy> {
  return apiRequest('/api/chat/upload-policy', {
    schema: uploadPolicySchema,
    ...(options.owner === undefined ? {} : { owner: options.owner }),
    ...(options.signal === undefined ? {} : { signal: options.signal }),
  })
}

export function chatUploadLimit(filename: string, policy: UploadPolicy): number {
  // Match POSIX Path(filename.lstrip('.')).suffix.lower(), including hidden names and trailing dots.
  const name = filename.replace(/^\.+/, '').split('/').filter((part) => part !== '' && part !== '.').at(-1) ?? ''
  const dot = name.lastIndexOf('.')
  const extension = dot > 0 ? name.slice(dot).toLowerCase() : ''
  return policy.image_extensions.includes(extension) ? policy.chat_image_max_bytes : policy.chat_file_max_bytes
}

export function formatUploadLimit(bytes: number): string {
  return bytes < 1024 * 1024 ? `${bytes.toLocaleString()} bytes` : `${bytes / (1024 * 1024)} MiB`
}
