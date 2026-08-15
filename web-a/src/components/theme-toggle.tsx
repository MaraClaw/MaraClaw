import { Moon, Sun } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { useTheme } from '@/hooks/use-theme'

export function ThemeToggle({ className }: { className?: string }) {
  const { resolvedTheme, toggleTheme } = useTheme()
  const isDark = resolvedTheme === 'dark'

  return (
    <Button
      type="button"
      variant="outline"
      size="icon"
      className={className}
      onClick={toggleTheme}
      aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
      title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
    >
      <Sun
        className="size-4 scale-100 rotate-0 transition-[transform,opacity] duration-200 dark:scale-0 dark:-rotate-90 dark:opacity-0"
        aria-hidden
        strokeWidth={1.75}
      />
      <Moon
        className="absolute size-4 scale-0 rotate-90 opacity-0 transition-[transform,opacity] duration-200 dark:scale-100 dark:rotate-0 dark:opacity-100"
        aria-hidden
        strokeWidth={1.75}
      />
    </Button>
  )
}
