import type { LinkupKey } from './linkup-keys-api.ts'

export function linkupKeyPresentation(
  key: Pick<LinkupKey, 'status' | 'exhausted_until'>,
  now: number,
) {
  const coolingDown = key.status === 'active' && key.exhausted_until !== null &&
    Date.parse(key.exhausted_until) > now
  return {
    label: coolingDown ? 'Cooldown' : key.status.charAt(0).toUpperCase() + key.status.slice(1),
    variant: coolingDown ? 'warning' : key.status === 'active' ? 'success' : 'secondary',
    cooldownUntil: coolingDown ? key.exhausted_until : null,
  } as const
}
