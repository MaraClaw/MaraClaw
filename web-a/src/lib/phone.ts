// `max` metadata matches the engine's `phonenumbers` validity rules; the default `min` bundle accepts numbers the engine rejects.
import {
  getCountries,
  getCountryCallingCode,
  parsePhoneNumberFromString,
  type CountryCode,
} from 'libphonenumber-js/max'

export const DEFAULT_PHONE_COUNTRY: CountryCode = 'VN'

export function phoneCountryOptions(locale: string) {
  const regionNames = new Intl.DisplayNames([locale], { type: 'region' })
  const collator = new Intl.Collator(locale)
  return getCountries()
    .map((country) => {
      const callingCode = getCountryCallingCode(country)
      return { country, callingCode, label: `${regionNames.of(country) ?? country} (+${callingCode})` }
    })
    .sort((a, b) => collator.compare(a.label, b.label))
}

/** Compact E.164 for a valid number, or `null`. A leading "+" overrides `country`. */
export function toE164(country: CountryCode, national: string): string | null {
  const phone = parsePhoneNumberFromString(national, country)
  return phone?.isValid() ? phone.number : null
}

export function formatPhone(e164: string): string {
  return parsePhoneNumberFromString(e164)?.formatInternational() ?? e164
}
