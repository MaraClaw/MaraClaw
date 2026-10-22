import { useId } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { deleteTool } from '@/lib/tools-api'
import { toolRecordImpact } from './tools-catalog-state'
import { toolButtonClass } from './tools-fields'
import type { CatalogTool } from './tools-forms'
import { useToolsAction } from './use-tools-action'

export function ToolDeleteConfirmation({ tool, onClose, onDeleted }: {
  readonly tool: CatalogTool; readonly onClose: () => void; readonly onDeleted: () => void
}) {
  const id = useId()
  const mutation = useToolsAction(() => deleteTool(tool.id), {
    success: 'Tool deleted', failure: 'Could not delete this tool. Check your permissions and try again.', onDone: onDeleted,
  })
  if (tool.type === 'builtin') return null
  return <Card role="region" aria-labelledby={`${id}-title`} className="min-w-0">
    <CardHeader>
      <CardTitle id={`${id}-title`} className="min-w-0 [overflow-wrap:anywhere]">Delete {tool.display_name}?</CardTitle>
      <CardDescription>{toolRecordImpact(tool)} Deleting it permanently removes the record and its agent tool assignments. This cannot be undone.</CardDescription>
    </CardHeader>
    <CardContent className="space-y-4">
      {mutation.errorMessage ? <p role="alert" className="text-sm text-destructive">{mutation.errorMessage}</p> : null}
      <div className="flex flex-wrap gap-3">
        <Button variant="destructive" className={toolButtonClass} disabled={mutation.isPending} onClick={() => mutation.submit(undefined)}>{mutation.isPending ? 'Deleting…' : 'Delete tool permanently'}</Button>
        <Button variant="outline" className={toolButtonClass} disabled={mutation.isPending} onClick={onClose}>Cancel</Button>
      </div>
    </CardContent>
  </Card>
}
