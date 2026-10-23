import { QueryObserver, type FetchQueryOptions, type QueryClient } from '@tanstack/react-query'

/** Last-view cancellation aborts the shared request; one closing view cannot cancel another. */
export async function requestAttachment<Data>(
  client: QueryClient, options: FetchQueryOptions<Data>, signal: AbortSignal,
): Promise<Data> {
  signal.throwIfAborted()
  const fresh = { ...options, staleTime: 0, gcTime: 0, retry: false }
  const observer = new QueryObserver(client, { ...fresh, enabled: false })
  const unsubscribe = observer.subscribe(() => {})
  return new Promise<Data>((resolve, reject) => {
    const abort = () => { unsubscribe(); reject(signal.reason) }
    signal.addEventListener('abort', abort, { once: true })
    void client.fetchQuery(fresh).then(resolve, reject).finally(() => {
      signal.removeEventListener('abort', abort)
      unsubscribe()
    })
  })
}
