import { useId } from 'react'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { preservedField, type ConfigField, type FieldEdit } from '@/lib/tool-config-fields'

export function AgentToolField({ field, edit = preservedField, overridden, inherited, inheritedDisplay, overrideDisplay, disabled, onChange }: {
  readonly field: ConfigField; readonly edit?: FieldEdit
  readonly overridden: boolean; readonly inherited: boolean; readonly disabled: boolean
  readonly inheritedDisplay: string; readonly overrideDisplay: string
  readonly onChange: (edit: FieldEdit) => void
}) {
  const id = useId()
  return <fieldset disabled={disabled} className="min-w-0 space-y-2">
    <legend className="text-sm font-medium [overflow-wrap:anywhere]">{field.label}</legend>
    <p className="text-sm text-muted-foreground">{overridden ? 'Agent override is saved.' : inherited ? 'Using an inherited value.' : 'No saved value.'}</p>
    <dl className="space-y-1 text-sm text-muted-foreground">
      <div className="break-words"><dt className="inline font-medium">Inherited: </dt><dd className="inline">{inheritedDisplay}</dd></div>
      <div className="break-words"><dt className="inline font-medium">Agent override: </dt><dd className="inline">{overrideDisplay}</dd></div>
    </dl>
    <Select aria-label={`${field.label} operation`} value={edit.operation} onChange={(event) => {
      switch (event.target.value) {
        case 'preserve': onChange({ operation: 'preserve', value: '' }); break
        case 'set': onChange({ operation: 'set', value: '' }); break
        case 'unset': onChange({ operation: 'unset', value: '' }); break
      }
    }}>
      <option value="preserve">Keep saved value</option>
      <option value="set">Set agent override</option>
      <option value="unset">Remove agent override</option>
    </Select>
    {edit.operation === 'set' ? <div className="space-y-2">
      <label htmlFor={id} className="text-sm">New {field.label}</label>
      {field.type === 'select' || field.type === 'boolean' ? <Select id={id} value={edit.value} onChange={(event) => onChange({ operation: 'set', value: event.target.value })}>
        <option value="">Choose a value</option>
        {field.type === 'boolean' ? <><option value="true">Yes</option><option value="false">No</option></> : field.options?.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </Select> : <Input id={id} className="h-11" type={field.type === 'password' ? 'password' : field.type === 'number' ? 'number' : 'text'} value={edit.value} autoComplete="off" data-private={field.type === 'password' ? 'true' : undefined} placeholder={field.placeholder} onChange={(event) => onChange({ operation: 'set', value: event.target.value })} />}
    </div> : null}
    {edit.operation === 'unset' ? <p className="text-sm text-muted-foreground">Removing the agent override reveals the inherited value, if one exists.</p> : null}
  </fieldset>
}
