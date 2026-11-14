import { Check, Copy } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { useTranslation } from '@/hooks/use-translation'
import { cn } from '@/lib/utils'
import { capacityActionClass } from './capacity-labels'

export function CopyField({ label, value, copyValue = value, large = false, className }: {
  readonly label: string
  readonly value: string
  readonly copyValue?: string
  readonly large?: boolean
  readonly className?: string
}) {
  const { t } = useTranslation()
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(timer.current), [])

  async function copy() {
    try {
      await navigator.clipboard.writeText(copyValue)
      setState('copied')
    } catch {
      setState('failed')
    }
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setState('idle'), 3000)
  }

  return <div className={cn('min-w-0', className)}>
    <dt className="text-xs text-muted-foreground">{label}</dt>
    <dd className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
      <span className={cn('min-w-0 font-medium tabular-nums [overflow-wrap:anywhere]', large ? 'font-display text-3xl tracking-tight' : 'text-base')}>{value}</span>
      <Button type="button" variant="outline" className={capacityActionClass} aria-label={t('Copy {label}', { label })} onClick={() => { void copy() }}>
        {state === 'copied' ? <Check aria-hidden="true" className="size-4" /> : <Copy aria-hidden="true" className="size-4" />}
        {t(state === 'copied' ? 'Copied' : 'Copy')}
      </Button>
      <span role="status" className={state === 'failed' ? 'basis-full text-sm text-destructive' : 'sr-only'}>
        {state === 'copied' ? t('{label} copied', { label }) : state === 'failed' ? t('Could not copy. Select the text and copy it yourself.') : ''}
      </span>
    </dd>
  </div>
}
