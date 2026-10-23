import { cva } from 'class-variance-authority'

export const badgeVariants = cva(
  'inline-flex items-center justify-center rounded-full border px-2.5 py-0.5 text-xs font-medium w-fit whitespace-nowrap shrink-0 gap-1.5 transition-[color,background-color,border-color] overflow-hidden',
  {
    variants: {
      variant: {
        default: 'border-transparent bg-primary text-primary-foreground',
        secondary: 'border-border bg-secondary text-secondary-foreground',
        outline: 'border-border bg-card text-foreground',
        soft: 'border-primary/25 bg-primary/10 text-primary',
        success: 'border-transparent bg-emerald-500/12 text-emerald-700 dark:text-emerald-300',
        destructive: 'border-transparent bg-destructive/12 text-destructive',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  },
)
