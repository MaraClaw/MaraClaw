export function formatMoney(amountMinor: number, currency: string, locale?: string) {
  const formatter = new Intl.NumberFormat(locale, { style: 'currency', currency })
  const exponent = formatter.resolvedOptions().maximumFractionDigits ?? 0
  return formatter.format(amountMinor / (10 ** exponent))
}
