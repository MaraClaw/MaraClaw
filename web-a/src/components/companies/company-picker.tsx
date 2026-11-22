import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { useId, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import {
  companyOptionByIdOptions,
  companyOptionsBrowseOptions,
  companyOptionsSearchOptions,
  isCompanyId,
} from '@/hooks/company-options'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { useTranslation } from '@/hooks/use-translation'
import type { CompanyOption } from '@/lib/companies-api'
import { cn } from '@/lib/utils'

/**
 * `pending`: a well-formed id whose by-id lookup has not answered (or is still disabled).
 * `unknown`: not a UUID (never sent), or the lookup returned 404. Only `resolved` may enable writes.
 */
export type CompanyPickerStatus = 'none' | 'pending' | 'resolved' | 'unknown' | 'error'

type CompanyPickerInput = {
  readonly value: string
  readonly enabled?: boolean
  /** Users and Models: with no selection, pick the own company once resolved by id, else the first loaded one. */
  readonly preselect?: { readonly ownCompanyId: string | null | undefined }
}

export type CompanyPickerState = {
  readonly value: string
  readonly status: CompanyPickerStatus
  readonly company: CompanyOption | null
  readonly defaultCompanyId: string | null
  readonly search: string
  readonly setSearch: (search: string) => void
  readonly query: string
  readonly loaded: readonly CompanyOption[]
  readonly loading: boolean
  readonly noCompanies: boolean
  readonly canLoadMore: boolean
  readonly loadingMore: boolean
  readonly loadMore: () => Promise<boolean>
  readonly error: Error | null
  readonly retry: () => void
}

const SEARCH_DEBOUNCE_MS = 300

function distinct(rows: readonly CompanyOption[]): CompanyOption[] {
  const byId = new Map<string, CompanyOption>()
  for (const row of rows) {
    if (!byId.has(row.id)) byId.set(row.id, row)
  }
  return [...byId.values()]
}

export function useCompanyPicker({ value, enabled = true, preselect }: CompanyPickerInput): CompanyPickerState {
  const [search, setSearch] = useState('')
  const query = useDebouncedValue(search, SEARCH_DEBOUNCE_MS).trim()
  const browse = useInfiniteQuery({ ...companyOptionsBrowseOptions(), enabled })
  const found = useQuery({ ...companyOptionsSearchOptions(query), enabled })
  const byId = useQuery({ ...companyOptionByIdOptions(value), enabled })
  const ownCompanyId = preselect?.ownCompanyId ?? ''
  const choosingDefault = enabled && preselect !== undefined && value === ''
  const own = useQuery({ ...companyOptionByIdOptions(ownCompanyId), enabled: choosingDefault })

  const status: CompanyPickerStatus = !value
    ? 'none'
    : !isCompanyId(value) || byId.data === null
      ? 'unknown'
      : byId.data
        ? 'resolved'
        : byId.isError ? 'error' : 'pending'
  const loaded = useMemo(
    () => distinct(query ? found.data?.items ?? [] : browse.data?.pages.flatMap((page) => page.items) ?? []),
    [query, found.data, browse.data],
  )
  const firstLoaded = browse.data?.pages[0]?.items[0]?.id ?? null
  const ownLookupFailed = !isCompanyId(ownCompanyId) || own.data === null || own.isError

  return {
    value,
    status,
    company: status === 'resolved' ? byId.data ?? null : null,
    defaultCompanyId: choosingDefault ? own.data?.id ?? (ownLookupFailed ? firstLoaded : null) : null,
    search,
    setSearch,
    query,
    loaded,
    loading: enabled && (query ? found.isPending : browse.isPending),
    noCompanies: browse.data?.pages[0]?.items.length === 0,
    canLoadMore: !query && browse.hasNextPage,
    loadingMore: browse.isFetchingNextPage,
    loadMore: async () => (await browse.fetchNextPage()).hasNextPage,
    error: browse.error ?? (query ? found.error : null) ?? byId.error,
    retry: () => {
      for (const lookup of [browse, found, byId]) {
        if (lookup.isError) void lookup.refetch()
      }
    },
  }
}

type CompanyPickerProps = {
  readonly picker: CompanyPickerState
  readonly label: string
  readonly unknownLabel: string
  readonly onChange: (companyId: string) => void
  /** Adds a first option with an empty value, such as "Entire system" or "All companies". */
  readonly emptyLabel?: string
  readonly selectLabel?: string
  readonly describedBy?: string
  readonly className?: string
  readonly labelClassName?: string
}

/** Native select over browse pages or ranked search results, with the selected company always pinned among the options. */
export function CompanyPicker({
  picker,
  label,
  unknownLabel,
  onChange,
  emptyLabel,
  selectLabel,
  describedBy,
  className,
  labelClassName,
}: CompanyPickerProps) {
  const { t } = useTranslation()
  const id = useId()
  const selectRef = useRef<HTMLSelectElement | null>(null)
  const { value, status, company, loaded, query } = picker
  const selectedName = status === 'resolved' && company
    ? company.name
    : status === 'unknown' ? unknownLabel : status === 'error' ? value : t('Loading company…')
  const selectedOffList = value !== '' && !loaded.some((option) => option.id === value)
  const message = picker.loading
    ? t('Loading companies…')
    : query && loaded.length === 0 ? t('No companies match “{search}”.', { search: query }) : ''

  return (
    <div className={cn('grid min-w-0 content-start gap-2', className)}>
      <Label htmlFor={`${id}-select`} className={labelClassName}>{label}</Label>
      <Input
        id={`${id}-search`}
        type="search"
        value={picker.search}
        aria-label={t('Search companies by name')}
        aria-controls={`${id}-select`}
        placeholder={t('Search companies by name')}
        onChange={(event) => picker.setSearch(event.target.value)}
        onKeyDown={(event) => {
          // Searching filters the menu; it never submits the surrounding filter form.
          if (event.key === 'Enter') event.preventDefault()
        }}
      />
      <Select
        ref={selectRef}
        id={`${id}-select`}
        value={value}
        aria-label={selectLabel}
        aria-describedby={describedBy}
        onChange={(event) => onChange(event.target.value)}
      >
        {emptyLabel !== undefined ? <option value="">{emptyLabel}</option> : null}
        {emptyLabel === undefined && value === ''
          ? <option value="" disabled>{picker.loading ? t('Loading companies…') : t('Choose a company')}</option>
          : null}
        {selectedOffList ? <option value={value}>{selectedName}</option> : null}
        {loaded.map((option) => <option key={option.id} value={option.id}>{option.name}</option>)}
      </Select>
      <p role="status" className="text-sm text-muted-foreground empty:sr-only">{message}</p>
      {picker.canLoadMore ? (
        <Button
          type="button"
          variant="outline"
          className="justify-self-start"
          disabled={picker.loadingMore}
          aria-busy={picker.loadingMore}
          onClick={async () => {
            // The button disappears with the last page; keep focus on the menu it extended.
            if (!(await picker.loadMore())) selectRef.current?.focus()
          }}
        >
          {t('Load more companies')}
        </Button>
      ) : null}
    </div>
  )
}
