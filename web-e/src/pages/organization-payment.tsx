import { useEffect, useId, useRef, useState } from 'react'
import type { ChangeEvent, ReactNode, SyntheticEvent } from 'react'

import { CopyField } from '@/components/copy-field'
import { SiteFooter } from '@/components/layout/site-footer'
import { SiteHeader } from '@/components/layout/site-header'
import { BackAnchor } from '@/components/ui/back-button'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { useTranslation } from '@/hooks/use-translation'
import { authSession } from '@/lib/auth-session'
import { userFacingRequestError } from '@/lib/http'
import type { TranslationKey } from '@/lib/i18n/catalog'
import {
  isOrganizationQuoteLapsed, organizationAdmissionErrorKey, RECEIPT_ACCEPT, receiptFileProblem,
} from '@/lib/organization-admission-errors'
import { submitOrganizationAdmission } from '@/lib/organization-admissions-api'
import type { OrganizationQuote } from '@/lib/organization-admissions-api'
import { clearOrganizationQuote, readOrganizationQuote } from '@/lib/organization-quote-storage'
import { formatPhone } from '@/lib/phone'

type Outcome =
  | { readonly kind: 'sent'; readonly reference: string; readonly email: string }
  | { readonly kind: 'lapsed' }

const usd: Intl.NumberFormatOptions = { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }
const vnd: Intl.NumberFormatOptions = { style: 'currency', currency: 'VND', maximumFractionDigits: 0 }
const bodyText = 'text-[17px] leading-relaxed sm:text-base'

function OutcomePanel({ heading, message, focusOnMount, children }: {
  readonly heading: string
  readonly message: string
  readonly focusOnMount: boolean
  readonly children?: ReactNode
}) {
  const headingRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => { if (focusOnMount) headingRef.current?.focus() }, [focusOnMount])
  return (
    <Card>
      <CardHeader>
        <h2 ref={headingRef} tabIndex={-1} className="font-display text-xl font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring">{heading}</h2>
        <p role="status" className={bodyText}>{message}</p>
      </CardHeader>
      {children ? <CardContent>{children}</CardContent> : null}
    </Card>
  )
}

function PaymentInstructions({ quote }: { readonly quote: OrganizationQuote }) {
  const { t: translate, formatNumber, formatDate } = useTranslation()
  return (
    <Card>
      <CardHeader>
        <h2 className="font-display text-xl font-semibold">{translate("Transfer this amount")}</h2>
        <p className={`${bodyText} text-muted-foreground`}>{translate("Make a bank transfer to the account below, then send your receipt. Your organization is created once a platform admin approves the payment.")}</p>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <dl className="grid gap-5 sm:grid-cols-2">
          <CopyField className="sm:col-span-2" large label={translate("Amount")} value={formatNumber(quote.amount_vnd, vnd)} copyValue={String(quote.amount_vnd)} />
          <CopyField label={translate("Bank name")} value={quote.recipient.bank_name} />
          <CopyField label={translate("Account number")} value={quote.recipient.account_number} />
          <CopyField label={translate("Branch name")} value={quote.recipient.branch_name} />
          <CopyField label={translate("Transfer note")} value={quote.reference} />
        </dl>
        <p className={`${bodyText} tabular-nums text-muted-foreground`}>
          {translate('{usd} at {rate} VND per USD', { usd: formatNumber(quote.amount_usd, usd), rate: formatNumber(quote.usd_vnd_rate) })}</p>
        <p className={`${bodyText} font-medium`}>{translate('Write {reference} in the transfer note so we can match your payment.', { reference: quote.reference })}</p>
        <p className={bodyText}>{translate('This price is valid until {date}.', { date: formatDate(quote.expires_at, { dateStyle: 'long', timeStyle: 'short' }) })}</p>
      </CardContent>
    </Card>
  )
}

function SummaryField({ label, children }: { readonly label: string; readonly children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className={`mt-1 ${bodyText}`}>{children}</dd>
    </div>
  )
}

function QuoteSummary({ quote }: { readonly quote: OrganizationQuote }) {
  const { t: translate, formatNumber } = useTranslation()
  return (
    <Card>
      <CardHeader>
        <h2 className="font-display text-xl font-semibold">{translate("Request summary")}</h2>
      </CardHeader>
      <CardContent>
        <dl className="grid gap-4 sm:grid-cols-2">
          <SummaryField label={translate("Organization name")}>{quote.organization_name}</SummaryField>
          <SummaryField label={translate("Email address")}>{quote.email}</SummaryField>
          <SummaryField label={translate("Phone number")}><span className="tabular-nums">{formatPhone(quote.phone)}</span></SummaryField>
          <SummaryField label={translate("Plan")}>{quote.plan.name}</SummaryField>
          <SummaryField label={translate("Billing period")}>{quote.billing === 'annual' ? translate("Annual") : translate("Monthly")}</SummaryField>
          <SummaryField label={translate("Amount in USD")}><span className="tabular-nums">{formatNumber(quote.amount_usd, usd)}</span></SummaryField>
          <SummaryField label={translate("Exchange rate")}><span className="tabular-nums">{translate('{rate} VND per USD', { rate: formatNumber(quote.usd_vnd_rate) })}</span></SummaryField>
          <SummaryField label={translate("Amount in VND")}><span className="tabular-nums">{formatNumber(quote.amount_vnd, vnd)}</span></SummaryField>
        </dl>
      </CardContent>
    </Card>
  )
}

function ReceiptForm({ quote, onSent, onLapsed }: {
  readonly quote: OrganizationQuote
  readonly onSent: (outcome: Extract<Outcome, { kind: 'sent' }>) => void
  readonly onLapsed: () => void
}) {
  const { t: translate, owned, formatNumber } = useTranslation()
  const id = useId()
  const [file, setFile] = useState<File | null>(null)
  const [problem, setProblem] = useState<TranslationKey | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  function fileSize(bytes: number): string {
    const plain = (value: number) => formatNumber(value, { maximumFractionDigits: 1 })
    if (bytes >= 1024 * 1024) return `${plain(bytes / (1024 * 1024))} MiB`
    if (bytes >= 1024) return `${plain(bytes / 1024)} KiB`
    return `${plain(bytes)} B`
  }

  function choose(event: ChangeEvent<HTMLInputElement>) {
    const chosen = event.currentTarget.files?.item(0) ?? null
    setFile(chosen)
    setProblem(chosen === null ? null : receiptFileProblem(chosen))
    setFailure(null)
  }

  async function submit(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault()
    if (file === null || problem !== null || pending) return
    const owner = authSession.capture()
    setPending(true)
    setFailure(null)
    try {
      const acknowledgment = await submitOrganizationAdmission(quote.quote_token, file)
      if (!authSession.isCurrent(owner)) return
      clearOrganizationQuote()
      onSent({ kind: 'sent', reference: acknowledgment.reference, email: quote.email })
    } catch (cause) {
      if (!authSession.isCurrent(owner)) return
      if (isOrganizationQuoteLapsed(cause)) {
        clearOrganizationQuote()
        onLapsed()
        return
      }
      setFailure(organizationAdmissionErrorKey(cause) ?? userFacingRequestError(cause, translate("We could not send your request. Please try again.")))
    } finally {
      setPending(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <h2 className="font-display text-xl font-semibold">{translate("Send your payment receipt")}</h2>
        <p className={`${bodyText} text-muted-foreground`}>{translate("After you transfer, attach a screenshot, photo or PDF of the receipt and select Submit request. A platform admin then checks the transfer.")}</p>
      </CardHeader>
      <CardContent>
        <form noValidate onSubmit={(event) => { void submit(event) }} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor={`${id}-file`} className="text-[17px] sm:text-base">{translate("Payment receipt")}</Label>
            <input id={`${id}-file`} type="file" accept={RECEIPT_ACCEPT} disabled={pending} onChange={choose}
              aria-invalid={problem !== null} aria-describedby={`${id}-hint${problem === null ? '' : ` ${id}-problem`}`}
              className="block min-h-11 w-full min-w-0 rounded-xl border border-input bg-card px-3 py-2.5 text-[17px] text-foreground shadow-sm file:me-3 file:rounded-lg file:border-0 file:bg-secondary file:px-3 file:py-1.5 file:text-[17px] file:font-medium file:text-secondary-foreground focus-visible:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/35 aria-invalid:border-destructive disabled:opacity-50 sm:text-base sm:file:text-base" />
            <p id={`${id}-hint`} className="text-sm text-muted-foreground">{translate("PNG, JPEG, WebP or PDF, up to 10 MiB.")}</p>
            {file !== null && problem === null && <p className={`${bodyText} tabular-nums`}>{translate('Selected: {name} ({size})', { name: file.name, size: fileSize(file.size) })}</p>}
            {problem !== null && <p id={`${id}-problem`} role="alert" className={`${bodyText} text-destructive`}>{owned(problem)}</p>}
          </div>
          {failure !== null && <p role="alert" className={`${bodyText} text-destructive`}>{owned(failure)}</p>}
          <p role="status" className={pending ? `${bodyText} text-muted-foreground` : 'sr-only'}>{pending ? translate("Sending your request and receipt.") : ''}</p>
          <Button type="submit" disabled={file === null || problem !== null || pending} aria-busy={pending} className="min-h-11 self-start text-base">
            {pending ? translate("Sending...") : translate("Submit request")}
          </Button>
        </form>
      </CardContent>
    </Card>
  )
}

export function OrganizationPaymentPage() {
  const { t: translate } = useTranslation()
  const [quote] = useState(() => readOrganizationQuote())
  const [outcome, setOutcome] = useState<Outcome | null>(null)

  let content: ReactNode
  if (outcome?.kind === 'sent') {
    content = (
      <OutcomePanel focusOnMount heading={translate("Request received")}
        message={translate('We received your request and payment receipt with reference {reference}. A platform admin will check your transfer. Once it is approved, your organization is created and the initial sign-in credentials are emailed to {email}.', { reference: outcome.reference, email: outcome.email })} />
    )
  } else if (quote === null || outcome?.kind === 'lapsed') {
    content = (
      <OutcomePanel focusOnMount={outcome !== null} heading={translate("Start your request again")}
        message={outcome === null
          ? translate("This browser has no valid price quote. Choose a plan and select Create organization to get a new one.")
          : translate("This price quote is no longer valid. Choose a plan and select Create organization to get a new one.")}>
        <Button asChild className="min-h-11 text-base"><a href="/#pricing">{translate("Start again")}</a></Button>
      </OutcomePanel>
    )
  } else {
    content = (
      <>
        <PaymentInstructions quote={quote} />
        <QuoteSummary quote={quote} />
        <ReceiptForm quote={quote} onSent={setOutcome} onLapsed={() => setOutcome({ kind: 'lapsed' })} />
      </>
    )
  }

  return (
    <div className="min-h-svh bg-background text-foreground">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-4 focus:left-4 focus:z-50 focus:rounded-lg focus:bg-primary focus:px-4 focus:py-2 focus:text-primary-foreground"
      >
        {translate("Skip to content")}</a>
      <SiteHeader linksToLanding />
      <main id="main" className="container-page py-10 sm:py-14">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
          <header className="flex flex-col gap-3">
            <span className="self-start"><BackAnchor href="/#pricing" label={translate("Back to pricing")} /></span>
            <h1 className="font-display text-3xl font-semibold tracking-tight sm:text-4xl">{translate("Pay for your organization")}</h1>
          </header>
          {content}
        </div>
      </main>
      <SiteFooter linksToLanding />
    </div>
  )
}
