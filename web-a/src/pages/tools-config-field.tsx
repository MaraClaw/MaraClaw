import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { useTranslation } from '@/hooks/use-translation'
import { preservedField, type ConfigField, type FieldEdit } from './tools-config-fields'

export function ToolConfigField({ field, edit = preservedField, stored, disabled, onChange }: {
  readonly field: ConfigField; readonly edit?: FieldEdit; readonly stored: boolean
  readonly disabled: boolean; readonly onChange: (edit: FieldEdit) => void
}) {
  const { t } = useTranslation()
  const id = useId()
  return <fieldset disabled={disabled} className="min-w-0 space-y-2">
    <legend className="text-sm font-medium [overflow-wrap:anywhere]">{field.label}</legend>
    <p className="text-sm text-muted-foreground">{stored ? t('A saved value exists.') : t('No value saved on this layer.')}</p>
    <Select aria-label={t('{label} operation', { label: field.label })} value={edit.operation} onChange={(event) => {
      switch (event.target.value) {
        case 'preserve': onChange({ operation: 'preserve', value: '' }); break
        case 'set': onChange({ operation: 'set', value: '' }); break
        case 'unset': onChange({ operation: 'unset', value: '' }); break
      }
    }}>
      <option value="preserve">{t('Keep saved value')}</option>
      <option value="set">{t('Set new value')}</option>
      <option value="unset">{t('Remove value from this layer')}</option>
    </Select>
    {edit.operation === 'set' ? <label htmlFor={id} className="block space-y-2 text-sm">
      <span>{t('New {label}', { label: field.label })}</span>
      {field.type === 'select' || field.type === 'boolean' ? <Select id={id} value={edit.value} onChange={(event) => onChange({ operation: 'set', value: event.target.value })}>
        <option value="">{t('Choose a value')}</option>
        {field.type === 'boolean' ? <><option value="true">{t('Yes')}</option><option value="false">{t('No')}</option></> : field.options?.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </Select> : <Input id={id} type={field.type === 'password' ? 'password' : field.type === 'number' ? 'number' : 'text'} value={edit.value} autoComplete="off" data-private={field.type === 'password' ? 'true' : undefined} placeholder={field.placeholder} onChange={(event) => onChange({ operation: 'set', value: event.target.value })} />}
    </label> : null}
    {edit.operation === 'unset' ? <p className="text-sm text-muted-foreground">{t('The saved value on this layer will be removed. An inherited value may become effective.')}</p> : null}
  </fieldset>
}
import { useId } from 'react'
