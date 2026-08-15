import { cn } from '@/lib/utils'

const ICONS = {
  agents: '/nav-icons/agents.png',
  plaza: '/nav-icons/plaza.png',
  okr: '/nav-icons/okr.png',
  directory: '/nav-icons/directory.png',
  inbox: '/nav-icons/inbox.png',
  account: '/nav-icons/account.png',
  settings: '/nav-icons/settings.png',
} as const

export type NavIconName = keyof typeof ICONS

export function NavIcon({
  name,
  className,
}: {
  name: NavIconName
  className?: string
}) {
  return (
    <img
      src={ICONS[name]}
      alt=""
      width={24}
      height={24}
      aria-hidden
      draggable={false}
      className={cn('size-6 shrink-0 select-none object-contain', className)}
    />
  )
}
