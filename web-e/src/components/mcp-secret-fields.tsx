import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { McpSecretField } from '@/lib/mcp-server-draft'

export function McpSecretFields({ label, fields, onChange }: {
  readonly label: string
  readonly fields: readonly McpSecretField[]
  readonly onChange: (fields: readonly McpSecretField[]) => void
}) {
  return (
    <fieldset className="min-w-0 space-y-3">
      <legend className="text-base font-medium">{label}</legend>
      {fields.map((field, index) => (
        <div key={field.id} className="grid min-w-0 gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end">
          <div className="grid min-w-0 gap-2">
            <Label htmlFor={`${field.id}-key`}>{label === 'Headers' ? 'Header name' : 'Variable name'} {index + 1}</Label>
            <Input id={`${field.id}-key`} value={field.key} required readOnly={field.stored}
              autoComplete="off" spellCheck={false}
              onChange={(event) => onChange(fields.map((item) => item.id === field.id ? { ...item, key: event.target.value } : item))} />
          </div>
          <div className="grid min-w-0 gap-2">
            <Label htmlFor={`${field.id}-value`}>{field.stored ? 'Replacement value' : 'Value'} {index + 1}</Label>
            <Input id={`${field.id}-value`} type="password" value={field.value} autoComplete="new-password" spellCheck={false}
              aria-describedby={field.stored ? `${field.id}-help` : undefined}
              onChange={(event) => onChange(fields.map((item) => item.id === field.id ? { ...item, value: event.target.value } : item))} />
          </div>
          <Button type="button" variant="outline" aria-label={`Remove ${label === 'Headers' ? 'header' : 'variable'} ${index + 1}`}
            onClick={() => onChange(fields.filter((item) => item.id !== field.id))}>Remove</Button>
          {field.stored ? <p id={`${field.id}-help`} className="text-sm text-muted-foreground sm:col-span-3">Leave the replacement blank to keep the saved value.</p> : null}
        </div>
      ))}
      <Button type="button" variant="outline" onClick={() => onChange([...fields, { id: crypto.randomUUID(), key: '', value: '', stored: false }])}>
        {label === 'Headers' ? 'Add header' : 'Add variable'}
      </Button>
    </fieldset>
  )
}
