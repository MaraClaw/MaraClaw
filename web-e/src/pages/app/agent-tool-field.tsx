import { useTranslation } from '@/hooks/use-translation'
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
  const { t: translate } = useTranslation()
  const id = useId()
  return <fieldset disabled={disabled} className="min-w-0 space-y-2">
    <legend className="text-sm font-medium [overflow-wrap:anywhere]">{field.label}</legend>
    <p className="text-sm text-muted-foreground">{overridden ? translate("Agent override is saved.") : inherited ? translate("Using an inherited value.") : translate("No saved value.")}</p>
    <dl className="space-y-1 text-sm text-muted-foreground">
      <div className="break-words"><dt className="inline font-medium">{translate("Inherited:")} </dt><dd className="inline">{inheritedDisplay}</dd></div>
      <div className="break-words"><dt className="inline font-medium">{translate("Agent override:")}{' '}</dt><dd className="inline">{overrideDisplay}</dd></div>
    </dl>
    <Select aria-label={translate('{field} operation', { field: field.label })} value={edit.operation} onChange={(event) => {
      switch (event.target.value) {
        case 'preserve': onChange({ operation: 'preserve', value: '' }); break
        case 'set': onChange({ operation: 'set', value: '' }); break
        case 'unset': onChange({ operation: 'unset', value: '' }); break
      }
    }}>
      <option value="preserve">{translate("Keep saved value")}</option>
      <option value="set">{translate("Set agent override")}</option>
      <option value="unset">{translate("Remove agent override")}</option>
    </Select>
    {edit.operation === 'set' ? <div className="space-y-2">
      <label htmlFor={id} className="text-sm">{translate("New")}{' '}{field.label}</label>
      {field.type === 'select' || field.type === 'boolean' ? <Select id={id} value={edit.value} onChange={(event) => onChange({ operation: 'set', value: event.target.value })}>
        <option value="">{translate("Choose a value")}</option>
        {field.type === 'boolean' ? <><option value="true">{translate("Yes")}</option><option value="false">{translate("No")}</option></> : field.options?.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </Select> : <Input id={id} className="h-11" type={field.type === 'password' ? 'password' : field.type === 'number' ? 'number' : 'text'} value={edit.value} autoComplete="off" data-private={field.type === 'password' ? 'true' : undefined} placeholder={field.placeholder} onChange={(event) => onChange({ operation: 'set', value: event.target.value })} />}
    </div> : null}
    {edit.operation === 'unset' ? <p className="text-sm text-muted-foreground">{translate("Removing the agent override reveals the inherited value, if one exists.")}</p> : null}
  </fieldset>
}
