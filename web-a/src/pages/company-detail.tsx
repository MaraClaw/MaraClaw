import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  addEmailDomain,
  deleteEmailDomain,
  listCompanies,
  listEmailDomains,
  setDefaultEmailDomain,
} from '@/lib/companies-api'
import { ApiError } from '@/lib/http'

export function CompanyDetailPage() {
  const { companyId } = useParams<{ companyId: string }>()
  const queryClient = useQueryClient()
  const [domain, setDomain] = useState('')

  const companies = useQuery({ queryKey: ['admin-companies'], queryFn: listCompanies })
  const company = companies.data?.find((item) => item.id === companyId)
  const domains = useQuery({
    queryKey: ['email-domains', companyId],
    queryFn: () => listEmailDomains(companyId!),
    enabled: Boolean(companyId),
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['email-domains', companyId] })
  }

  const add = useMutation({
    mutationFn: () => addEmailDomain(companyId!, domain, (domains.data?.length ?? 0) === 0),
    onSuccess: () => {
      setDomain('')
      invalidate()
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : 'Could not add domain'),
  })
  const makeDefault = useMutation({
    mutationFn: (domainId: string) => setDefaultEmailDomain(companyId!, domainId),
    onSuccess: invalidate,
    onError: (error) => toast.error(error instanceof ApiError ? error.message : 'Could not update default'),
  })
  const remove = useMutation({
    mutationFn: (domainId: string) => deleteEmailDomain(companyId!, domainId),
    onSuccess: invalidate,
    onError: (error) => toast.error(error instanceof ApiError ? error.message : 'Could not delete domain'),
  })

  if (!companyId) return null

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6">
      <Button asChild variant="ghost" className="w-fit px-0">
        <Link to="/companies">← Companies</Link>
      </Button>

      <div>
        <h1 className="font-display text-3xl font-semibold tracking-tight">{company?.name ?? 'Company'}</h1>
        <p className="mt-2 text-muted-foreground">
          Claimed email domains route new registrations to this organization.
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          {company?.is_system ? <Badge variant="secondary">System</Badge> : null}
          {company?.is_default_end_user_org ? <Badge>Default for end users</Badge> : null}
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Email domains</CardTitle>
          <CardDescription>One domain can belong to only one company. Mark one as default for invites.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <form
            className="flex flex-wrap gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              if (domain.trim()) add.mutate()
            }}
          >
            <Input
              value={domain}
              onChange={(event) => setDomain(event.target.value)}
              placeholder="acme.com"
              className="max-w-xs"
            />
            <Button type="submit" disabled={add.isPending || !domain.trim()}>
              Add domain
            </Button>
          </form>

          {(domains.data ?? []).map((item) => (
            <div key={item.id} className="flex flex-wrap items-center justify-between gap-2 border-b py-2 last:border-0">
              <div className="flex items-center gap-2">
                <span className="font-mono text-sm">{item.domain}</span>
                {item.is_default ? <Badge variant="outline">Default</Badge> : null}
              </div>
              <div className="flex gap-2">
                {!item.is_default ? (
                  <Button variant="outline" size="sm" onClick={() => makeDefault.mutate(item.id)}>
                    Make default
                  </Button>
                ) : null}
                <Button variant="ghost" size="sm" onClick={() => remove.mutate(item.id)}>
                  Remove
                </Button>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
