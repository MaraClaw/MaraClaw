import { cn } from '@/lib/utils'

type MaraClawMarkProps = {
  className?: string
  title?: string
}

/** Brand mark asset shared with web-l (`public/maraclaw-mark.svg`). */
export function MaraClawMark({ className, title = 'MaraClaw' }: MaraClawMarkProps) {
  return (
    <img
      src="/maraclaw-mark.svg"
      alt={title}
      className={cn('size-8 shrink-0', className)}
      width={32}
      height={32}
      decoding="async"
    />
  )
}
