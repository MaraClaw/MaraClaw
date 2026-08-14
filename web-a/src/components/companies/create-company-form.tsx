import { zodResolver } from '@hookform/resolvers/zod'
import { Loader2 } from 'lucide-react'
import { useId, useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PasswordField } from '@/components/ui/password-field'
import { createCompany } from '@/lib/companies-api'
import { ApiError, formatApiDetail } from '@/lib/http'

const schema = z.object({
  name: z.string().trim().min(1, 'Enter a company name').max(200, 'Name is too long'),
  admin_email: z
    .string()
    .trim()
    .min(1, 'Enter the org admin email')
    .email('Enter a valid email address')
    .max(254, 'Email is too long'),
  admin_display_name: z.string().trim().max(200, 'Display name is too long').optional(),
  admin_password: z.string().min(6, 'Use at least 6 characters').max(128, 'Password is too long'),
})

type FormValues = z.infer<typeof schema>

type CreateCompanyFormProps = {
  onCreated: (name: string, adminEmail: string) => void
}

export function CreateCompanyForm({ onCreated }: CreateCompanyFormProps) {
  const formId = useId()
  const [formError, setFormError] = useState<string | null>(null)
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: '',
      admin_email: '',
      admin_display_name: '',
      admin_password: '',
    },
  })

  async function onSubmit(values: FormValues) {
    setFormError(null)
    const displayName = values.admin_display_name?.trim()
    try {
      const created = await createCompany({
        name: values.name.trim(),
        admin_email: values.admin_email.trim().toLowerCase(),
        admin_password: values.admin_password,
        admin_display_name: displayName || undefined,
      })
      reset()
      onCreated(created.company.name, created.org_admin_email)
    } catch (error) {
      if (error instanceof ApiError) {
        if (error.status === 409) {
          setFormError(
            formatApiDetail(error.detail) ??
              'That admin email or email domain is already registered.',
          )
          return
        }
        if (error.status === 403) {
          setFormError('Only a platform admin can create a company.')
          return
        }
        setFormError(formatApiDetail(error.detail) ?? 'Could not create company.')
        return
      }
      setFormError('Could not create company. Check your connection and try again.')
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>New company</CardTitle>
        <CardDescription>
          Creates the organization and its genesis org admin. The admin email host is claimed as
          this company&apos;s default email domain. That admin must change the initial password on
          first login.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form className="grid gap-4 sm:grid-cols-2" onSubmit={handleSubmit(onSubmit)} noValidate>
          <div className="space-y-2 sm:col-span-2">
            <Label htmlFor={`${formId}-name`}>Company name</Label>
            <Input
              id={`${formId}-name`}
              autoComplete="organization"
              aria-invalid={errors.name ? true : undefined}
              {...register('name')}
            />
            {errors.name ? (
              <p className="text-xs text-destructive" role="alert">
                {errors.name.message}
              </p>
            ) : null}
          </div>
          <div className="space-y-2">
            <Label htmlFor={`${formId}-email`}>Org admin email</Label>
            <Input
              id={`${formId}-email`}
              type="email"
              autoComplete="off"
              aria-invalid={errors.admin_email ? true : undefined}
              {...register('admin_email')}
            />
            {errors.admin_email ? (
              <p className="text-xs text-destructive" role="alert">
                {errors.admin_email.message}
              </p>
            ) : null}
          </div>
          <div className="space-y-2">
            <Label htmlFor={`${formId}-display`}>Admin display name (optional)</Label>
            <Input
              id={`${formId}-display`}
              autoComplete="off"
              {...register('admin_display_name')}
            />
            {errors.admin_display_name ? (
              <p className="text-xs text-destructive" role="alert">
                {errors.admin_display_name.message}
              </p>
            ) : null}
          </div>
          <div className="sm:col-span-2">
            <PasswordField
              id={`${formId}-password`}
              label="Initial admin password"
              autoComplete="new-password"
              hideLeadingIcon
              error={errors.admin_password?.message}
              {...register('admin_password')}
            />
          </div>
          {formError ? (
            <p className="text-sm text-destructive sm:col-span-2" role="alert">
              {formError}
            </p>
          ) : null}
          <div className="sm:col-span-2">
            <Button type="submit" disabled={isSubmitting}>
              {isSubmitting ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
              Create company
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}
