import { useQuery } from '@tanstack/react-query'
import { useId } from 'react'
import { useSearchParams } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/hooks/use-auth'
import { listCompanies } from '@/lib/companies-api'
import { isPlatformAdminUser } from '@/lib/types/auth'
import { ToolsCatalog } from './tools-catalog'
import { toolButtonClass } from './tools-fields'
import { toolsTenantId } from './tools-forms'

export function ToolsPage() {
  const id = useId()
  const { user, owner } = useAuth()
  const [params, setParams] = useSearchParams()
  const platform = isPlatformAdminUser(user)
  const companies = useQuery({ queryKey: ['admin-companies'], queryFn: ({ signal }) => listCompanies(undefined, signal), enabled: platform })
  const tenantId = toolsTenantId(user, params.get('company'))
  const concreteCompany = !!tenantId && (!platform || !!companies.data?.some((company) => company.id === tenantId))
  return <div className="mx-auto flex w-full min-w-0 max-w-5xl flex-col gap-6">
    <header className="space-y-2">
      <h1 className="font-display text-2xl font-semibold tracking-tight">Tools</h1>
      <p className="max-w-3xl text-sm leading-relaxed text-muted-foreground">Manage the live tool catalog, MCP connections and configuration. Built-in and global records are shared across companies. Company-owned records affect only their owning company and agents assigned to the tool.</p>
    </header>
    <Card><CardContent className="space-y-4">
      {platform ? <div className="max-w-lg space-y-2">
        <Label htmlFor={`${id}-company`}>Company context</Label>
        <Select id={`${id}-company`} value={tenantId ?? ''} disabled={companies.isPending} aria-describedby="tools-company-help" onChange={(event) => {
          const next = new URLSearchParams(params)
          if (event.target.value) next.set('company', event.target.value)
          else next.delete('company')
          setParams(next)
        }}>
          <option value="">Current / platform scope</option>
          {tenantId && !companies.data?.some((company) => company.id === tenantId) ? <option value={tenantId}>Unrecognized company selection</option> : null}
          {companies.data?.map((company) => <option key={company.id} value={company.id}>{company.name}</option>)}
        </Select>
        {companies.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading companies…</p> : null}
        {companies.isError ? <div className="space-y-2"><p role="alert" className="text-sm text-destructive">Could not load companies.</p><Button variant="outline" className={toolButtonClass} onClick={() => { void companies.refetch() }}>Retry loading companies</Button></div> : null}
      </div> : <p className="text-sm font-medium">Company context: your organization</p>}
      <p id="tools-company-help" className="text-sm leading-relaxed text-muted-foreground">{concreteCompany
        ? 'This selection scopes catalog visibility, new MCP tools and built-in source configuration. Each record shows its ownership; company selection does not change who owns it. Available actions follow the permissions returned by the server for this context.'
        : 'Inspect record ownership and server-permitted actions in this catalog. Select a concrete company before creating MCP tools or replacing configuration. Current / platform scope uses the server’s current context, which may include multiple companies.'}</p>
    </CardContent></Card>
    <ToolsCatalog key={`${owner.generation}:${tenantId ?? 'current'}:${concreteCompany}`} tenantId={tenantId} concreteCompany={concreteCompany} />
  </div>
}
