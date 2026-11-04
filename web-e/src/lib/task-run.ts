import { userFacingRequestError } from './http.ts'

export function canRunTask(status: string | null | undefined): boolean {
  return status !== 'doing'
}

export function taskRunErrorMessage(error: unknown): string {
  return userFacingRequestError(error, 'Unable to run task')
}
