import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { userFacingRequestError } from '@/lib/http'
import { RequestErrorToast } from '@/components/request-error-toast'

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
    {error ? <div className="space-y-2">
      <RequestErrorToast error={error} message={userFacingRequestError(error, ready ? 'Could not refresh this conversation.' : 'Could not load this conversation.')} />
      <Button type="button" variant="outline" onClick={onRefresh}>Retry conversation</Button>
    </div> : null}
    {ready ? <>
      {earlierError ? <RequestErrorToast error={earlierError} message="Could not load earlier messages. You can keep chatting." /> : null}
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
