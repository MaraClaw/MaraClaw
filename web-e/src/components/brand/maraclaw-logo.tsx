import { useId } from 'react'

import { cn } from '@/lib/utils'

type MaraClawLogoProps = {
  className?: string
  /** full = branded gradient tile; mark = monochrome currentColor glyph */
  variant?: 'full' | 'mark'
  title?: string
}

/**
 * MaraClaw brand mark: geometric claw gripping an AI spark-node.
 * Designed to stay legible at favicon and header sizes.
 */
export function MaraClawLogo({
  className,
  variant = 'full',
  title = 'MaraClaw',
}: MaraClawLogoProps) {
  const uid = useId().replace(/:/g, '')

  if (variant === 'mark') {
    return (
      <svg
        xmlns="http://www.w3.org/2000/svg"
        viewBox="0 0 64 64"
        fill="none"
        className={cn('size-6 text-primary', className)}
        role="img"
        aria-label={title}
      >
        <title>{title}</title>
        <g fill="currentColor">
          <circle cx="28.5" cy="36.5" r="7.2" />
          <path d="M31.2 30.6C36.2 24.8 42.4 20.2 49.2 18.4C50.8 18 52 19.6 51.2 21.1C48.4 26.2 44.1 30.4 38.8 33.4C36.6 34.7 33.8 34.1 31.8 32.4C31.2 31.9 31 31.2 31.2 30.6Z" />
          <path d="M33.4 39.2C39.8 40.6 45.6 44.2 49.8 49.6C50.8 50.9 49.6 52.6 48 52C41.8 49.6 36.4 45.4 32.4 40.2C31.4 38.9 32.1 38.8 33.4 39.2Z" />
          <path d="M22.6 33.4C17.2 29.8 12.8 24.6 10.4 18.6C9.8 17.1 11.4 15.8 12.8 16.6C18.2 19.6 22.6 24.2 25.4 29.6C26.4 31.5 25 33.4 22.6 33.4Z" />
          <circle cx="24.8" cy="41.8" r="3.4" />
        </g>
        <g transform="translate(40.5 31.5)">
          <circle r="4.4" fill="currentColor" opacity="0.22" />
          <path
            d="M0 -2.6 L0.7 -0.7 L2.6 0 L0.7 0.7 L0 2.6 L-0.7 0.7 L-2.6 0 L-0.7 -0.7 Z"
            fill="currentColor"
          />
        </g>
      </svg>
    )
  }

  const bgId = `mc-bg-${uid}`
  const shineId = `mc-shine-${uid}`

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 64 64"
      className={cn('size-9', className)}
      role="img"
      aria-label={title}
    >
      <title>{title}</title>
      <defs>
        <linearGradient id={bgId} x1="12" y1="8" x2="54" y2="58" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#E8894F" />
          <stop offset="55%" stopColor="#D06635" />
          <stop offset="100%" stopColor="#A84522" />
        </linearGradient>
        <linearGradient id={shineId} x1="18" y1="14" x2="40" y2="42" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#FFF6EE" stopOpacity="0.95" />
          <stop offset="100%" stopColor="#F3D2B8" stopOpacity="0.88" />
        </linearGradient>
      </defs>
      <rect width="64" height="64" rx="16" fill={`url(#${bgId})`} />
      <rect
        x="2.5"
        y="2.5"
        width="59"
        height="59"
        rx="13.5"
        fill="none"
        stroke="#FFF8F2"
        strokeOpacity="0.18"
        strokeWidth="1"
      />
      <g fill={`url(#${shineId})`}>
        <circle cx="28.5" cy="36.5" r="7.2" />
        <path d="M31.2 30.6C36.2 24.8 42.4 20.2 49.2 18.4C50.8 18 52 19.6 51.2 21.1C48.4 26.2 44.1 30.4 38.8 33.4C36.6 34.7 33.8 34.1 31.8 32.4C31.2 31.9 31 31.2 31.2 30.6Z" />
        <path d="M33.4 39.2C39.8 40.6 45.6 44.2 49.8 49.6C50.8 50.9 49.6 52.6 48 52C41.8 49.6 36.4 45.4 32.4 40.2C31.4 38.9 32.1 38.8 33.4 39.2Z" />
        <path d="M22.6 33.4C17.2 29.8 12.8 24.6 10.4 18.6C9.8 17.1 11.4 15.8 12.8 16.6C18.2 19.6 22.6 24.2 25.4 29.6C26.4 31.5 25 33.4 22.6 33.4Z" />
        <circle cx="24.8" cy="41.8" r="3.4" />
      </g>
      <g transform="translate(40.5 31.5)">
        <circle r="4.4" fill="#FFF8F2" />
        <path
          d="M0 -2.6 L0.7 -0.7 L2.6 0 L0.7 0.7 L0 2.6 L-0.7 0.7 L-2.6 0 L-0.7 -0.7 Z"
          fill="#C25A2C"
        />
      </g>
    </svg>
  )
}
