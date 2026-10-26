import { useState } from 'react'

import type { NotificationRow } from '../lib/notifications-api.ts'

export function useNotificationSelection(items: readonly NotificationRow[], context: object) {
  const [selection, setSelection] = useState<{ readonly context: object; readonly ids: readonly string[] }>({ context, ids: [] })
  const ids = selection.context === context ? selection.ids.filter((id) => items.some((item) => item.id === id)) : []
  if (selection.context !== context || ids.length !== selection.ids.length) setSelection({ context, ids })

  return {
    ids,
    clear: () => setSelection({ context, ids: [] }),
    toggle: (id: string) => setSelection((current) => ({ context, ids: current.ids.includes(id) ? current.ids.filter((value) => value !== id) : current.ids.length < 200 ? [...current.ids, id] : current.ids })),
    complete: () => setSelection((current) => current === selection ? { context, ids: [] } : current),
    loadedMore: () => setSelection((current) => current.context === context ? { context, ids: [] } : current),
  }
}
