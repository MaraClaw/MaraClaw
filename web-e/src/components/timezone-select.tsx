import type { ComponentProps } from 'react'

import { Select } from '@/components/ui/select'
import { timezoneOptions } from '@/lib/timezone-catalog'

export function TimezoneSelect({ value, ...props }: Omit<ComponentProps<typeof Select>, 'value' | 'children'> & { readonly value: string }) {
  return <Select {...props} value={value}>
    {timezoneOptions(value).map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
  </Select>
}
