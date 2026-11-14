import { Button } from '@/components/ui/button'
import { useTranslation } from '@/hooks/use-translation'
import type { Locale } from '@/lib/i18n/translation'
import { cn } from '@/lib/utils'

const locales = ['en', 'vi'] as const satisfies readonly Locale[]
const flags = { en: '/flags/us.svg', vi: '/flags/vn.svg' } as const satisfies Record<Locale, string>

/** Icon button matching ThemeToggle; the flag shows the active language. */
export function LanguageSelector({ className }: { className?: string }) {
  const { locale, setLocale, t } = useTranslation()
  const label = locale === 'en'
    ? t('Language: English. Switch to Tiếng Việt')
    : t('Language: Tiếng Việt. Switch to English')

  return (
    <Button
      type="button"
      variant="outline"
      size="icon"
      className={className}
      onClick={() => setLocale(locale === 'en' ? 'vi' : 'en')}
      aria-label={label}
      title={label}
    >
      {locales.map((code) => (
        <img
          key={code}
          src={flags[code]}
          alt=""
          width={20}
          height={14}
          draggable={false}
          className={cn(
            'absolute h-3.5 w-5 rounded-[3px] object-cover outline-1 -outline-offset-1 outline-black/10 transition-[transform,opacity] duration-200 dark:outline-white/15',
            code === locale ? 'scale-100 rotate-0 opacity-100' : 'scale-0 rotate-90 opacity-0',
          )}
        />
      ))}
    </Button>
  )
}
