import type { QueryClient, UseMutationOptions } from '@tanstack/react-query'
import type { AuthSessionStore, SessionOwner } from '../lib/auth-session.ts'

export class RetiredWorkspaceError extends Error {
  constructor() { super('Workspace session retired'); this.name = 'RetiredWorkspaceError' }
}

/** The client and owner are captured together; never resolve a replacement client later. */
export function createSessionQueryScope(store: AuthSessionStore, owner: SessionOwner, client: QueryClient) {
  const isCurrent = () => store.isCurrent(owner)
  const assertCurrent = () => { if (!isCurrent()) throw new RetiredWorkspaceError() }
  const rethrowCurrent = (error: unknown) => { if (isCurrent()) throw error }
  function effect<Args extends unknown[], Result>(callback: (...args: Args) => Result) {
    return (...args: Args): Result | undefined => {
      if (!isCurrent()) return
      return callback(...args)
    }
  }
  async function run<Result>(callback: () => Result | PromiseLike<Result>): Promise<Result> {
    assertCurrent()
    const result = await callback()
    assertCurrent()
    return result
  }
  function subscribe() {
    const retire = () => {
      if (isCurrent()) return
      void client.cancelQueries()
      client.clear()
    }
    const unsubscribe = store.subscribe(retire)
    retire()
    // StrictMode's rehearsal cleanup must not retire a still-current workspace.
    return unsubscribe
  }
  return { owner, client, isCurrent, assertCurrent, rethrowCurrent, effect, run, subscribe }
}

export type SessionQueryScope = ReturnType<typeof createSessionQueryScope>

export function fenceMutationOptions<Data, ErrorType, Variables, Context>(
  scope: SessionQueryScope,
  options: UseMutationOptions<Data, ErrorType, Variables, Context>,
): UseMutationOptions<Data, ErrorType, Variables, Context> {
  const { mutationFn, onMutate, onSuccess, onError, onSettled } = options
  return {
    ...options,
    ...(mutationFn && { mutationFn: (variables, context) => scope.run(() => mutationFn(variables, context)) }),
    ...(onMutate && { onMutate: (variables, context) => scope.run(() => onMutate(variables, context)) }),
    ...(onSuccess && { onSuccess: scope.effect(onSuccess) }),
    ...(onError && { onError: scope.effect(onError) }),
    ...(onSettled && { onSettled: scope.effect(onSettled) }),
  }
}
