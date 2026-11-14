import { localeTag, type Locale } from './i18n.ts'

export function formatVnd(amount: number, locale: Locale): string {
  return `${new Intl.NumberFormat(localeTag[locale]).format(amount)} VND`
}

export function formatUsdCents(cents: number, locale: Locale): string {
  return new Intl.NumberFormat(localeTag[locale], { style: 'currency', currency: 'USD' }).format(cents / 100)
}

export function formatUsd(dollars: number, locale: Locale): string {
  return formatUsdCents(dollars * 100, locale)
}

export function formatFileSize(bytes: number, locale: Locale): string {
  const plain = (value: number) => new Intl.NumberFormat(localeTag[locale], { maximumFractionDigits: 1 }).format(value)
  if (bytes >= 1024 * 1024) return `${plain(bytes / (1024 * 1024))} MiB`
  if (bytes >= 1024) return `${plain(bytes / 1024)} KiB`
  return `${plain(bytes)} B`
}

function calendar(value: string, locale: Locale, options: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat(localeTag[locale], { ...options, timeZone: 'UTC' }).format(new Date(`${value}T00:00:00Z`))
}

/** `YYYY-MM-DD` values are calendar days, not instants: format them in UTC so no time zone shifts the day. */
export function formatCalendarDate(value: string, locale: Locale): string {
  return calendar(value, locale, { day: 'numeric', month: 'long', year: 'numeric' })
}

export function formatBillingMonth(value: string, locale: Locale): string {
  return calendar(value, locale, { month: 'long', year: 'numeric' })
}
