import type { MutationFunction, UseMutationOptions } from '@tanstack/react-query'

import { runOwned, type SessionOwner, type SessionPolicy } from '../lib/session-policy.ts'

export type SessionMutationOptions<TData, TError, TVariables, TContext> =
  UseMutationOptions<TData, TError, TVariables, TContext> & {
    readonly mutationFn: MutationFunction<TData, TVariables>
  }

export function fenceCallback<TArgs extends readonly unknown[], TResult>(
  isCurrent: () => boolean,
  callback: ((...args: TArgs) => TResult) | undefined,
) {
  return (...args: TArgs) => isCurrent() ? callback?.(...args) : undefined
}

export function ownMutationOptions<TData, TError, TVariables, TContext>(
  policy: SessionPolicy,
  owner: SessionOwner,
  options: SessionMutationOptions<TData, TError, TVariables, TContext>,
): UseMutationOptions<TData, TError, TVariables, TContext> {
  const isCurrent = () => policy.isCurrent(owner)
  const onMutate = options.onMutate
  return {
    ...options,
    mutationFn: (variables, context) => runOwned(
      policy, owner, () => options.mutationFn(variables, context),
    ),
    onMutate: onMutate ? (variables, context) => {
      policy.assertCurrent(owner)
      return onMutate(variables, context)
    } : undefined,
    onSuccess: fenceCallback(isCurrent, options.onSuccess),
    onError: fenceCallback(isCurrent, options.onError),
    onSettled: fenceCallback(isCurrent, options.onSettled),
  }
}
