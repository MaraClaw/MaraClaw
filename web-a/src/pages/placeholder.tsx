import { Badge } from '@/components/ui/badge'
import { useTranslation } from '@/hooks/use-translation'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

type PlaceholderPageProps = {
  title: string
  description: string
  apiHint?: string
}

export function PlaceholderPage({ title, description, apiHint }: PlaceholderPageProps) {
  const { t, owned } = useTranslation()
  return (
    <div className="mx-auto w-full max-w-3xl">
      <Card>
        <CardHeader>
          <div className="mb-1">
            <Badge variant="secondary">{t('Placeholder')}</Badge>
          </div>
          <CardTitle className="text-2xl">{owned(title)}</CardTitle>
          <CardDescription>{owned(description)}</CardDescription>
        </CardHeader>
        {apiHint ? (
          <CardContent>
            <p className="text-sm text-muted-foreground">
              {t('API reference:')} <code className="rounded-md bg-muted px-1.5 py-0.5 text-xs">{apiHint}</code>
            </p>
          </CardContent>
        ) : null}
      </Card>
    </div>
  )
}
