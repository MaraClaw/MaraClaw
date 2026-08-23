import { Eye, EyeOff, Lock } from 'lucide-react'
import { useId, useState, type ComponentProps } from 'react'

import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'

type PasswordFieldProps = Omit<ComponentProps<'input'>, 'type' | 'id'> & {
  id?: string
  label: string
  error?: string
}

export function PasswordField({
  id,
  label,
  error,
  className,
  disabled,
  ...props
}: PasswordFieldProps) {
  const autoId = useId()
  const fieldId = id ?? autoId
  const errorId = `${fieldId}-error`
  const [show, setShow] = useState(false)

  return (
    <div className="space-y-2">
      <Label htmlFor={fieldId}>{label}</Label>
      <div className="relative">
        <Lock
          className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden
        />
        <Input
          id={fieldId}
          type={show ? 'text' : 'password'}
          className={cn('pr-11 pl-10', className)}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          disabled={disabled}
          {...props}
        />
        <button
          type="button"
          className="absolute top-1/2 right-2 flex size-9 -translate-y-1/2 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
          onClick={() => setShow((value) => !value)}
          aria-label={show ? 'Hide password' : 'Show password'}
          disabled={disabled}
        >
          {show ? <EyeOff className="size-4" aria-hidden /> : <Eye className="size-4" aria-hidden />}
        </button>
      </div>
      {error ? (
        <p id={errorId} className="text-xs text-destructive" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  )
}
