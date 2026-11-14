import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { useTranslation } from '@/hooks/use-translation'
import { downloadCsvFile } from '@/lib/csv-download'
import { ApiError } from '@/lib/http'
import {
  exportResourcePressureCsv,
  RESOURCE_PRESSURE_CSV_FILENAME,
  type ResourcePressureFilters,
} from '@/lib/resource-pressure-api'

/** Mount with a `key` that changes with `filters`, so a finished download's status never describes other filters. */
export function UpgradeSignalsCsvButton({ filters, accessibleName }: {
  readonly filters: ResourcePressureFilters
  readonly accessibleName?: string
}) {
  const { t } = useTranslation()
  const exporting = useSessionMutation({
    mutationFn: () => exportResourcePressureCsv(filters),
    onSuccess: (csv) => downloadCsvFile(csv, RESOURCE_PRESSURE_CSV_FILENAME),
    retry: false,
  })
  const message = exporting.isPending
    ? t('Preparing CSV…')
    : exporting.isSuccess ? t('CSV download started. Check your browser downloads.') : ''
  const error = exporting.isError
    ? exporting.error instanceof ApiError ? exporting.error.message : t('Could not export the CSV. Try again.')
    : null

  return (
    <div className="flex min-w-0 flex-col items-start gap-2">
      {/* Stays focusable while the export runs, so keyboard focus is not lost. */}
      <Button
        type="button"
        variant="outline"
        className="h-auto min-h-11 max-w-full whitespace-normal aria-disabled:pointer-events-none aria-disabled:opacity-50"
        aria-label={accessibleName}
        aria-disabled={exporting.isPending}
        onClick={() => { if (!exporting.isPending) exporting.mutate() }}
      >
        {t('Download CSV')}
      </Button>
      <p role="status" className="text-sm text-muted-foreground">{message}</p>
      {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
      {error ? <ErrorToast error={exporting.error} message={error} /> : null}
    </div>
  )
}
