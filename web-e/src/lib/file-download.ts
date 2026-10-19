type FilePreviewRequest = {
  readonly agentId: string
  readonly path: string
  readonly attempt: 0 | 1
}

export type FilePreviewState =
  | { readonly status: 'minting'; readonly request: FilePreviewRequest }
  | { readonly status: 'ready'; readonly request: FilePreviewRequest; readonly url: string }
  | { readonly status: 'failed'; readonly request: FilePreviewRequest }

type FilePreviewEvent =
  | { readonly type: 'minted'; readonly request: FilePreviewRequest; readonly url: string }
  | { readonly type: 'mint-failed' | 'image-error'; readonly request: FilePreviewRequest }

export function createFilePreview(agentId: string, path: string): FilePreviewState {
  return { status: 'minting', request: { agentId, path, attempt: 0 } }
}

export function reduceFilePreview(state: FilePreviewState, event: FilePreviewEvent): FilePreviewState {
  if (state.request !== event.request) return state

  switch (event.type) {
    case 'minted':
      return state.status === 'minting' ? { status: 'ready', request: state.request, url: event.url } : state
    case 'mint-failed':
      return state.status === 'minting' ? { status: 'failed', request: state.request } : state
    case 'image-error':
      if (state.status !== 'ready') return state
      return state.request.attempt === 0
        ? { status: 'minting', request: { ...state.request, attempt: 1 } }
        : { status: 'failed', request: state.request }
  }
}
