import { Check, Copy } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'

export function ResponseCopy({ text }: { readonly text: string }) {
  const [copy, setCopy] = useState({ source: '', status: '' })
  const copied = copy.source === text && copy.status === 'copied'
  useEffect(() => {
    if (copy.source !== text || copy.status !== 'copied') return
    const timeout = window.setTimeout(() => {
      setCopy((current) => current === copy ? { source: text, status: '' } : current)
    }, 2000)
    return () => window.clearTimeout(timeout)
  }, [copy, text])
  async function copyResponse() {
    setCopy({ source: text, status: '' })
    try {
      await navigator.clipboard.writeText(text)
      setCopy({ source: text, status: 'copied' })
    } catch (error) {
      if (!(error instanceof Error)) throw error
      setCopy({ source: text, status: 'failed' })
    }
  }
  return <div className="flex min-h-11 items-center justify-end gap-2 whitespace-normal">
    <span role="status" aria-live="polite" className="text-xs">{copy.source === text && copy.status === 'failed' ? 'Could not copy. Try again.' : copied ? 'Copied' : ''}</span>
    <Button type="button" variant="ghost" size="icon" className="size-11 text-inherit" aria-label={copied ? 'Copied response' : 'Copy response'} title={copied ? 'Copied response' : 'Copy response'} onClick={() => void copyResponse()}>
      {copied ? <Check className="size-4" aria-hidden /> : <Copy className="size-4" aria-hidden />}
    </Button>
  </div>
}
