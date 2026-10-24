import * as Dialog from '@radix-ui/react-dialog'
import { useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { formatMoney } from '@/lib/format-money'
import type { TransferQuote, TransferSelection } from '@/lib/types/company-transfers'

export function TransferChoice({ quote, busy, error, onCancel, onConfirm }: {
  readonly quote: TransferQuote
  readonly busy: boolean
  readonly error: string | null
  readonly onCancel: () => void
  readonly onConfirm: (selection: TransferSelection) => void
}) {
  const cancel = useRef<HTMLButtonElement>(null)
  const returnFocus = useRef<HTMLElement | null>(null)
  const [choice, setChoice] = useState<'paid' | 'free' | null>(null)
  const [consent, setConsent] = useState(false)
  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open && !busy) onCancel() }}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
        <Dialog.Content role="alertdialog" aria-modal="true"
          className="fixed top-1/2 left-1/2 z-50 max-h-[90svh] w-[calc(100%_-_2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 space-y-4 overflow-y-auto rounded-2xl border border-border bg-card p-6 shadow-elevated"
          onOpenAutoFocus={(event) => {
            event.preventDefault()
            returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
            cancel.current?.focus()
          }}
          onCloseAutoFocus={(event) => { event.preventDefault(); returnFocus.current?.focus() }}
          onPointerDownOutside={(event) => event.preventDefault()}
          onEscapeKeyDown={(event) => { if (busy) event.preventDefault() }}>
          <Dialog.Title className="font-display text-xl font-semibold">Choose how to transfer</Dialog.Title>
          <Dialog.Description className="text-sm text-muted-foreground">
            Nothing moves or is deleted until the destination company approves your admission. Your current workspace stays available while you wait.
          </Dialog.Description>
          <fieldset disabled={busy} className="space-y-3">
            <legend className="sr-only">Transfer options</legend>
            <label className="flex min-h-11 cursor-pointer gap-3 rounded-xl border border-border p-4 text-sm">
              <input type="radio" name="transfer-choice" value="paid" checked={choice === 'paid'} onChange={() => { setChoice('paid'); setConsent(false) }} className="mt-1 size-4 shrink-0 accent-primary" />
              <span className="space-y-1"><span className="block font-medium">Paid · Keep agents and integrations</span>
                <span className="block">{formatMoney(quote.fee_amount_minor, quote.currency)} ({quote.currency})</span>
                <span className="block text-muted-foreground">Keep your agents, integrations and content. Source organization models, tools and grants do not carry over; automation stops and needs reconfiguration.</span>
              </span>
            </label>
            <label className="flex min-h-11 cursor-pointer gap-3 rounded-xl border border-border p-4 text-sm">
              <input type="radio" name="transfer-choice" value="free" checked={choice === 'free'} onChange={() => { setChoice('free'); setConsent(false) }} className="mt-1 size-4 shrink-0 accent-primary" />
              <span className="space-y-1"><span className="block font-medium">Free · Delete agents and integrations</span>
                <span className="block text-muted-foreground">At final approval, your agents expire and become unavailable. Integrations, credentials, permissions, relationships, schedules, triggers and public pages are deleted.</span>
              </span>
            </label>
            {choice === 'free' ? <label className="flex min-h-11 cursor-pointer items-start gap-3 text-sm">
              <input type="checkbox" checked={consent} onChange={(event) => setConsent(event.currentTarget.checked)} className="mt-1 size-4 shrink-0 accent-primary" />
              <span>I explicitly agree to delete my agents and integrations when the destination approves. I understand this cannot be undone.</span>
            </label> : null}
          </fieldset>
          <p className="text-sm text-muted-foreground">Historical chats, tasks, audit and activity records, accounting, Plaza content, revisions, files and memory remain attributed to the source organization. With a free transfer they are retained for compliance, not erased, and you lose access.</p>
          {choice === 'paid' ? <div className="space-y-2 rounded-xl bg-muted/50 p-4 text-sm">
            <h2 className="font-medium">Payment instructions</h2>
            <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{quote.instructions || 'No payment instructions provided. Contact support before paying.'}</p>
            <p className="text-muted-foreground">Payment is reviewed first. Payment approval only creates an admission request; the destination company must approve separately. The submitted request saves the server’s fee and instructions; check its status before paying.</p>
          </div> : null}
          {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
          <div className="flex flex-wrap justify-end gap-2">
            <Button ref={cancel} variant="outline" className="min-h-11" disabled={busy} onClick={onCancel}>Cancel</Button>
            <Button variant={choice === 'free' ? 'secondary' : 'default'} className={`h-auto min-h-11 whitespace-normal ${choice === 'free' ? 'bg-destructive/12 text-destructive hover:bg-destructive/20' : ''}`} disabled={busy || choice === null || (choice === 'free' && !consent)}
              onClick={() => {
                switch (choice) {
                  case 'paid': onConfirm({ transfer_choice: 'paid' }); break
                  case 'free': if (consent) onConfirm({ transfer_choice: 'free', free_destructive_consent: true }); break
                  case null: break
                }
              }}>{busy ? 'Submitting…' : choice === 'paid' ? 'Request payment review' : choice === 'free' ? 'Confirm free transfer' : 'Choose an option'}</Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
