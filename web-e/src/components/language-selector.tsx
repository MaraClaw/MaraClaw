import { useTranslation } from '@/hooks/use-translation'

export function LanguageSelector() {
  const { locale, setLocale, t } = useTranslation()
  const label = locale === 'en'
    ? t('Language: English. Switch to Tiếng Việt')
    : t('Language: Tiếng Việt. Switch to English')
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={() => setLocale(locale === 'en' ? 'vi' : 'en')}
      className="relative flex h-10 w-12 shrink-0 items-center justify-center overflow-hidden rounded-lg border border-transparent outline-1 -outline-offset-1 outline-black/10 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background motion-safe:transition-[scale] motion-safe:duration-150 motion-safe:ease-out motion-safe:active:scale-[0.96] dark:outline-white/10"
    >
      <img src={locale === 'en' ? '/flags/us.svg' : '/flags/vn.svg'} alt="" className="absolute inset-0 size-full object-cover" />
      <span className="relative rounded bg-black/75 px-1 text-xs font-bold leading-5 text-white" lang={locale}>
        {locale === 'en' ? 'EN' : 'VI'}
      </span>
    </button>
  )
}
