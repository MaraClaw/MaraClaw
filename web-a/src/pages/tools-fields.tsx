import type { ReactNode } from 'react'

import { Label } from '@/components/ui/label'

export const toolTextareaClass = 'min-h-32 w-full min-w-0 resize-y rounded-xl border border-input bg-card px-3.5 py-2 text-base text-foreground shadow-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/35 aria-invalid:border-destructive sm:text-sm motion-reduce:transition-none'
export const toolButtonClass = 'h-auto min-h-11 whitespace-normal'

export function ToolField({ id, label, error, children }: {
  readonly id: string; readonly label: string; readonly error?: string; readonly children: ReactNode
}) {
  return <div className="min-w-0 space-y-2">
    <Label htmlFor={id}>{label}</Label>
    {children}
    {error ? <p id={`${id}-error`} role="alert" className="text-sm text-destructive">{error}</p> : null}
  </div>
}
