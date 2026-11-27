import { createElement, lazy, type ComponentType, type LazyExoticComponent } from 'react'

/** Shares one import while it is pending or loaded; a rejection is forgotten, so the next call issues a fresh `import()`. */
export function retryingImport<T>(load: () => Promise<T>): () => Promise<T> {
  let loading: Promise<T> | null = null
  return () => loading ??= load().catch((error: unknown) => {
    loading = null
    throw error
  })
}

/** A route's code failed to download. `retry` makes the next render of that route issue a fresh `import()`. */
export class ChunkLoadError extends Error {
  readonly retry: () => void

  constructor(retry: () => void, cause: unknown) {
    super(cause instanceof Error ? cause.message : String(cause), { cause })
    this.name = 'ChunkLoadError'
    this.retry = retry
  }
}

/**
 * `React.lazy` replays a rejected import for the life of the page, so a failed load rejects with a ChunkLoadError
 * whose `retry` swaps in a new lazy component: the route then downloads again without a full reload.
 */
export function retryingLazy(load: () => Promise<ComponentType>): ComponentType {
  const attempt = (): LazyExoticComponent<ComponentType> => {
    const current = lazy(() => load().then(
      (component) => ({ default: component }),
      (error: unknown) => {
        throw new ChunkLoadError(() => { if (route === current) route = attempt() }, error)
      },
    ))
    return current
  }
  let route = attempt()
  return function RetryingLazy() {
    return createElement(route)
  }
}
