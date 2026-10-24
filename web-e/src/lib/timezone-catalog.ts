type TimezoneOption = { readonly value: string; readonly label: string }

export const timezoneCatalog: readonly (TimezoneOption & { readonly offset: number })[] = Array.from({ length: 27 }, (_, index) => {
  const offset = index - 12
  return {
    offset,
    value: offset === 0 ? 'UTC' : `Etc/GMT${offset > 0 ? '-' : '+'}${Math.abs(offset)}`,
    label: `GMT ${offset >= 0 ? '+' : ''}${offset}`,
  }
})

export function timezoneLabel(timezone: string): string {
  return timezoneCatalog.find((option) => option.value === timezone)?.label ?? timezone
}

export function timezoneOptions(current: string): readonly TimezoneOption[] {
  return timezoneCatalog.some((option) => option.value === current)
    ? timezoneCatalog : [...timezoneCatalog, { value: current, label: current }]
}
