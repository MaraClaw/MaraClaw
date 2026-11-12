import { useId, useMemo, useState } from 'react'
import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useTranslation } from '@/hooks/use-translation'
import type { JsonObject } from '@/lib/tools-api'
import { ToolConfigField } from './tools-config-field'
import { configFields, fieldOperations, type FieldEdits } from './tools-config-fields'
import { toolButtonClass } from './tools-fields'
import type { CatalogTool } from './tools-forms'

export function ToolConfigOperationsEditor({ tool, pending, error, onSave, onProbe, onReplace, onClose }: {
  readonly tool: CatalogTool; readonly pending: boolean; readonly error: string | null
  readonly onSave: (operations: { readonly set: JsonObject; readonly unset: readonly string[] }) => void
  readonly onProbe?: (operations: { readonly set: JsonObject; readonly unset: readonly string[] }) => void
  readonly onReplace: () => void; readonly onClose: () => void
}) {
  const { t } = useTranslation()
  const id = useId()
  const fields = useMemo(() => configFields(tool.config_schema), [tool.config_schema])
  const [edits, setEdits] = useState<FieldEdits>({})
  const [validation, setValidation] = useState<string | null>(null)
  const operations = (submit: typeof onSave) => {
    try { const draft = fieldOperations(fields, edits); setValidation(null); submit(draft) }
    catch (error) { if (!(error instanceof Error)) throw error; setValidation(t('Enter a new value for every field marked Set. Masked and empty values cannot be saved.')) }
  }
  const changed = Object.values(edits).some((edit) => edit.operation !== 'preserve')
  return <Card role="region" aria-labelledby={`${id}-title`} className="min-w-0">
    <CardHeader>
      <CardTitle id={`${id}-title`} className="[overflow-wrap:anywhere]">{t('Configure {name}', { name: tool.display_name })}</CardTitle>
      <CardDescription>{t('Unchanged fields retain their saved values. Secret inputs start empty.')}</CardDescription>
      <p className="text-sm text-muted-foreground">{tool.source === 'builtin' ? t('Changes apply to the selected company settings.') : t('Changes apply to this catalog record.')}</p>
    </CardHeader>
    <CardContent>
      <form autoComplete="off" noValidate className="space-y-6" onSubmit={(event) => { event.preventDefault(); operations(onSave) }}>
        <div className="grid min-w-0 gap-6 sm:grid-cols-2">
          {fields.map((field) => <ToolConfigField key={field.key} field={field} edit={edits[field.key]} stored={Object.hasOwn(tool.config, field.key)} disabled={pending} onChange={(edit) => setEdits((previous) => ({ ...previous, [field.key]: edit }))} />)}
        </div>
        {fields.length === 0 ? <p className="text-sm text-muted-foreground">{t('No supported fields. Use the explicit whole replacement editor for this tool.')}</p> : null}
        {validation ? <p role="alert" className="text-sm text-destructive">{validation}</p> : null}
        {error ? <ErrorToast message={error} /> : null}
        <div className="flex flex-wrap gap-3">
          <Button type="submit" className={toolButtonClass} disabled={pending || !changed}>{pending ? t('Working…') : t('Save fields')}</Button>
          {onProbe ? <Button type="button" variant="outline" className={toolButtonClass} disabled={pending} onClick={() => operations(onProbe)}>{t('Test saved + draft')}</Button> : null}
          <Button type="button" variant="outline" className={toolButtonClass} disabled={pending} onClick={() => { setEdits({}); onClose() }}>{t('Cancel')}</Button>
        </div>
        <Button type="button" variant="outline" className={toolButtonClass} disabled={pending} onClick={() => { setEdits({}); onReplace() }}>{t('Replace entire configuration…')}</Button>
      </form>
    </CardContent>
  </Card>
}
