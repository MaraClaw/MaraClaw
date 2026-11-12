import { useTranslation } from '@/hooks/use-translation'
import { Link } from 'react-router-dom'

import type { AdmissionSummary } from '@/lib/types/company-admissions'

export function AdmissionPending({ request }: { readonly request: AdmissionSummary }) {
  const { t: translate } = useTranslation()
  return (
    <div role="status" className="mb-4 space-y-2 rounded-xl border border-border bg-muted/50 p-4 text-sm">
      <p className="font-medium">{translate("Awaiting review")}</p>
      <p className="text-muted-foreground">{translate("Your request to join")}{' '}{request.target_tenant.name} {translate("is pending. Your current organization has not changed.")}</p>
      <Link className="inline-block font-medium text-primary underline-offset-4 hover:underline" to={`/app/admissions/${encodeURIComponent(request.id)}`}>
        {translate("View admission request")}</Link>
    </div>
  )
}
