import { useTranslation } from '@/hooks/use-translation'
import { languageName, type Locale } from '@/lib/i18n'
import { cn } from '@/lib/utils'

const locales = ['en', 'vi'] as const satisfies readonly Locale[]
const flags = { en: '/flags/us.svg', vi: '/flags/vn.svg' } as const satisfies Record<Locale, string>
const otherLocale = { en: 'vi', vi: 'en' } as const satisfies Record<Locale, Locale>

/** Flag-backed switch between English and Vietnamese; the flag shows the active language. */
export function LanguageToggle({ className }: { readonly className?: string }) {
  const { t, locale, setLocale } = useTranslation()
  const label = t('Language: {current}. Switch to {next}', {
    current: languageName[locale],
    next: languageName[otherLocale[locale]],
  })

  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={() => setLocale(otherLocale[locale])}
      className={cn(
        'relative inline-flex h-10 w-14 shrink-0 items-center justify-center overflow-hidden rounded-xl border border-transparent shadow-sm outline-hidden transition-[scale,filter,box-shadow] duration-150 ease-out hover:brightness-105 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background motion-safe:active:scale-[0.96]',
        className,
      )}
    >
      {locales.map((code) => (
        <img
          key={code}
          src={flags[code]}
          alt=""
          width={28}
          height={20}
          draggable={false}
          className={cn(
            'absolute inset-0 size-full object-cover outline-1 -outline-offset-1 outline-black/10 dark:outline-white/10',
            code !== locale && 'hidden',
          )}
        />
      ))}
      <span lang={locale} className="relative rounded-md bg-black/70 px-1.5 py-0.5 text-xs leading-none font-semibold tracking-wide text-white">
        {locale.toUpperCase()}
      </span>
    </button>
  )
}
