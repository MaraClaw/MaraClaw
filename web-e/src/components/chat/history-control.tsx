import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { ApiError, formatApiDetail } from '@/lib/http'

type HistoryControlProps = {
  readonly ready: boolean
  readonly loading: boolean
  readonly error: unknown
  readonly hasEarlier: boolean
  readonly loadingEarlier: boolean
  readonly earlierError: unknown
  readonly onRefresh: () => void
  readonly onLoadEarlier: () => void
}

export function HistoryControl({ ready, loading, error, hasEarlier, loadingEarlier, earlierError, onRefresh, onLoadEarlier }: HistoryControlProps) {
  const [keyboardActive, setKeyboardActive] = useState(false)
  return <div className="space-y-2 text-center text-sm text-muted-foreground">
    {loading ? <p role="status">Loading conversation…</p> : null}
    {error ? <div role="alert" className="space-y-2 text-destructive">
      <p>{error instanceof ApiError ? formatApiDetail(error.detail) ?? error.message
        : ready ? 'Could not refresh this conversation.' : 'Could not load this conversation.'}</p>
      <Button type="button" variant="outline" onClick={onRefresh}>Retry conversation</Button>
    </div> : null}
    {ready ? <>
      {earlierError ? <p role="alert" className="text-destructive">Could not load earlier messages. You can keep chatting.</p> : null}
      {hasEarlier ? <Button type="button" variant="outline"
        disabled={loadingEarlier && !keyboardActive}
        aria-disabled={loadingEarlier && keyboardActive ? true : undefined}
        className="aria-disabled:pointer-events-none aria-disabled:opacity-50"
        onBlur={() => setKeyboardActive(false)}
        onClick={(event) => {
          if (loadingEarlier) return
          setKeyboardActive(event.detail === 0)
          onLoadEarlier()
        }}>
        {loadingEarlier ? 'Loading earlier messages…' : earlierError ? 'Retry earlier messages' : 'Load earlier messages'}
      </Button> : <p role="status">Beginning of conversation</p>}
      <span className="sr-only" role="status" aria-live="polite">{loadingEarlier ? 'Loading earlier messages' : ''}</span>
    </> : null}
  </div>
}
