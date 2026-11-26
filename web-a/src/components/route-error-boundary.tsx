import { Component, useTransition, type ReactNode } from 'react'
import { Outlet, useLocation, useOutletContext } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { useTranslation } from '@/hooks/use-translation'
import { ChunkLoadError } from '@/lib/retrying-import'

type BoundaryProps = { readonly resetKey: string; readonly children: ReactNode }
type BoundaryState = { readonly failure: { readonly error: unknown } | null; readonly resetKey: string }

function RouteError({ download, onRetry }: { readonly download: boolean; readonly onRetry: () => void }) {
  const { t } = useTranslation()
  const [retrying, startRetry] = useTransition()
  return (
    <div className="space-y-3">
      <div role="alert" className="space-y-1">
        <h2 className="font-display text-lg font-semibold">
          {download ? t('This page couldn’t be downloaded') : t('This page stopped working')}
        </h2>
        <p className="max-w-md text-sm text-muted-foreground">
          {download ? t('Check your connection and try again.') : t('Try again, or open another page.')}
        </p>
      </div>
      {/* A transition keeps this message on screen while the page downloads again. */}
      <Button type="button" variant="outline" onClick={() => startRetry(onRetry)}>
        {retrying ? t('Loading…') : t('Try again')}
      </Button>
    </div>
  )
}

class Boundary extends Component<BoundaryProps, BoundaryState> {
  state: BoundaryState = { failure: null, resetKey: this.props.resetKey }

  static getDerivedStateFromError(error: unknown): Partial<BoundaryState> {
    return { failure: { error } }
  }

  // Any navigation clears the failure, so the next route renders without a reload.
  static getDerivedStateFromProps(props: BoundaryProps, state: BoundaryState): Partial<BoundaryState> | null {
    return props.resetKey === state.resetKey ? null : { failure: null, resetKey: props.resetKey }
  }

  componentDidCatch(error: unknown) {
    // React.lazy keeps a failed import; the next render of this route (retry or a return visit) downloads it again.
    if (error instanceof ChunkLoadError) error.retry()
  }

  render() {
    const { failure } = this.state
    if (!failure) return this.props.children
    return <RouteError download={failure.error instanceof ChunkLoadError} onRetry={() => this.setState({ failure: null })} />
  }
}

/** Layout route inside the shell: a page that throws or fails to download leaves the surrounding navigation mounted. */
export function RouteErrorBoundary() {
  const { key } = useLocation()
  const context = useOutletContext()
  return <Boundary resetKey={key}><Outlet context={context} /></Boundary>
}
