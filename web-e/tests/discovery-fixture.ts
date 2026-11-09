import type { FileInfo } from '../src/lib/workspace-api.ts'

export const skillFile = (path: string, is_dir = false): FileInfo => ({
  name: path.split('/').at(-1) ?? '', path, is_dir, size: 0,
})

export class DiscoveryBodyResponse extends Response {
  readonly started = Promise.withResolvers<void>()
  readonly result = Promise.withResolvers<string>()
  override text(): Promise<string> {
    this.started.resolve()
    return this.result.promise
  }
}
