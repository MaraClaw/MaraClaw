import { useRef, useState } from 'react'

import type { NotificationRow } from '../lib/notifications-api.ts'

export function useNotificationSelection(items: readonly NotificationRow[], context: object) {
  const [selection, setSelection] = useState<{ readonly context: object; readonly ids: readonly string[] }>({ context, ids: [] })
  const owner = useRef(context)
  if (selection.context !== context) owner.current = context
  const submittedOwner = owner.current
  const ids = selection.context === context ? selection.ids.filter((id) => items.some((item) => item.id === id)) : []
  if (selection.context !== context || ids.length !== selection.ids.length) setSelection({ context, ids })

  return {
    ids,
    clear: () => { owner.current = {}; setSelection({ context, ids: [] }) },
    toggle: (id: string) => {
      owner.current = {}
      setSelection((current) => ({ context, ids: current.ids.includes(id) ? current.ids.filter((value) => value !== id) : current.ids.length < 200 ? [...current.ids, id] : current.ids }))
    },
    complete: () => {
      if (owner.current !== submittedOwner) return false
      owner.current = {}
      setSelection({ context, ids: [] })
      return true
    },
    loadedMore: () => {
      if (owner.current !== submittedOwner) return
      owner.current = {}
      setSelection((current) => current.context === context ? { context, ids: [] } : current)
    },
  }
}
