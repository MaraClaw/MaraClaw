import { z } from 'zod'

const boundError = 'Enter an RFC 3339 timestamp with seconds and an offset, e.g. 2026-10-21T12:30:00Z, or leave this blank.'
const rfc3339 = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$/
const datetime = z.iso.datetime({ offset: true })
const bound = z.string({ error: boundError }).trim().toUpperCase()
  .refine((value) => value === '' || (rfc3339.test(value) && !value.startsWith('0000-') && datetime.safeParse(value).success), boundError)
  .transform((value, context) => {
    if (value === '') return undefined
    const fraction = /\.([0-9]+)/.exec(value)?.[1] ?? ''
    const seconds = new Date(value.replace(/\.[0-9]+/, '')).toISOString().slice(0, -5)
    if (seconds.startsWith('0000-') || seconds.length !== 19) {
      context.addIssue({ code: 'custom', message: 'Enter a timestamp whose UTC year is between 0001 and 9999.' })
      return z.NEVER
    }
    return `${seconds}.${fraction.padEnd(6, '0').slice(0, 6)}Z`
  }).optional()

export const adminAuditWindowSchema = z.object({ start_at: bound, end_at: bound })
  .refine(({ start_at, end_at }) => start_at === undefined || end_at === undefined || start_at < end_at, {
    path: ['end_at'], message: 'End time must be later than start time. Start is inclusive; end is exclusive.',
  })
