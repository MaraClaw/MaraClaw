import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

type PlaceholderPageProps = {
  title: string
  description: string
  apiHint?: string
}

export function PlaceholderPage({ title, description, apiHint }: PlaceholderPageProps) {
  return (
    <div className="mx-auto w-full max-w-3xl">
      <Card>
        <CardHeader>
          <div className="mb-1">
            <Badge variant="secondary">Placeholder</Badge>
          </div>
          <CardTitle className="text-2xl">{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </CardHeader>
        {apiHint ? (
          <CardContent>
            <p className="text-sm text-muted-foreground">
              API reference: <code className="rounded-md bg-muted px-1.5 py-0.5 text-xs">{apiHint}</code>
            </p>
          </CardContent>
        ) : null}
      </Card>
    </div>
  )
}
