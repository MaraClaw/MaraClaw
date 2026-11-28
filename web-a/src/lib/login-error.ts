import { requestFailureMessage } from './error-toast.ts'
import { ApiError, formatApiDetail } from './http.ts'
import { message, type LocalMessage } from './i18n.ts'
import type { LoginFormValues } from './login-schema.ts'
import type { NeedsVerificationDetail } from './types/auth.ts'

export function parseLoginError(error: unknown): {
  formMessage: string | LocalMessage
  field?: keyof LoginFormValues
  needsVerification?: NeedsVerificationDetail
} {
  if (!(error instanceof ApiError)) {
    return { formMessage: requestFailureMessage(error, message('Unable to sign in. Check your connection and try again.')) }
  }

  const detail = error.detail

  if (detail && typeof detail === 'object' && 'needs_verification' in detail) {
    const v = detail as NeedsVerificationDetail
    return {
      formMessage: formatApiDetail(v.message) ?? message('Please verify your email to continue.'),
      needsVerification: v,
    }
  }

  if (error.status === 401) {
    return {
      formMessage: message('Email or password is incorrect.'),
      field: 'password',
    }
  }

  if (error.status === 403) {
    return { formMessage: formatApiDetail(detail) ?? message('Access denied for this account.') }
  }

  if (error.status === 404) {
    return {
      formMessage: formatApiDetail(detail) ?? message('No organization is linked to this account.'),
    }
  }

  return {
    formMessage: error.message,
  }
}
