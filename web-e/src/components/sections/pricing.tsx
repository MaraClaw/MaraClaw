import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'

import { RequestErrorToast } from '@/components/request-error-toast'
import { OrganizationAdmissionForm } from '@/components/sections/organization-admission-form'
import { Button } from '@/components/ui/button'
import { fetchOrganizationPlans } from '@/lib/organization-admissions-api'
import type { OrganizationPlan } from '@/lib/organization-admissions-api'
import { authSession } from '@/lib/auth-session'

type Catalog =
  | { readonly status: 'loading' }
  | { readonly status: 'error'; readonly error: unknown }
  | { readonly status: 'ready'; readonly plans: readonly OrganizationPlan[] }

function PricingCatalog({ retry }: { readonly retry: () => void }) {
  const [catalog, setCatalog] = useState<Catalog>({ status: 'loading' })

  useEffect(() => {
    const owner = authSession.capture()
    const controller = new AbortController()
    fetchOrganizationPlans(controller.signal).then(
      ({ plans }) => { if (!controller.signal.aborted && authSession.isCurrent(owner)) setCatalog({ status: 'ready', plans }) },
      (error: unknown) => { if (!controller.signal.aborted && authSession.isCurrent(owner)) setCatalog({ status: 'error', error }) },
    )
    return () => controller.abort()
  }, [])

  let content: ReactNode
  switch (catalog.status) {
    case 'loading':
      content = <p role="status" className="mt-10 text-center text-[17px] text-muted-foreground">Loading organization plans...</p>
      break
    case 'error':
      content = <div className="mt-10 space-y-4 text-center">
        <RequestErrorToast error={catalog.error} message="Organization plans could not be loaded." />
        <Button variant="outline" onClick={retry}>Retry plans</Button>
      </div>
      break
    case 'ready':
      content = <OrganizationAdmissionForm plans={catalog.plans} />
      break
    default:
      catalog satisfies never
  }
  return content
}

export function Pricing() {
  const [attempt, setAttempt] = useState(0)

  return (
    <section id="pricing" aria-labelledby="pricing-heading" className="scroll-mt-20 py-20 sm:py-28">
      <div className="container-page">
        <div className="mx-auto max-w-2xl text-center">
          <p className="text-sm font-semibold uppercase tracking-wider text-primary">Organization plans</p>
          <h2 id="pricing-heading" className="mt-3 font-display text-3xl font-semibold tracking-tight sm:text-4xl">Choose a plan for your team</h2>
          <p className="mt-4 text-[17px] leading-relaxed text-muted-foreground sm:text-lg">Proposed monthly pricing for human teams and their digital employees. Select a plan and request approval to get started.</p>
        </div>
        <PricingCatalog key={attempt} retry={() => setAttempt((value) => value + 1)} />
      </div>
    </section>
  )
}
