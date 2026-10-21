export function relativeTime(value: string, now = Date.now()): string {
  const seconds = (new Date(value).getTime() - now) / 1000
  const units = [
    ['year', 31_536_000], ['month', 2_592_000], ['day', 86_400],
    ['hour', 3600], ['minute', 60], ['second', 1],
  ] as const
  const [unit, duration] = units.find(([, size]) => Math.abs(seconds) >= size) ?? ['second', 1]
  return new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }).format(Math.round(seconds / duration), unit)
}
