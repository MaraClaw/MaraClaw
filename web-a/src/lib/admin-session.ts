import { clearStoredToken, getStoredToken, setStoredToken } from './auth-storage.ts'
import { createSessionPolicy } from './session-policy.ts'

export const adminSession = createSessionPolicy({
  read: getStoredToken,
  write: setStoredToken,
  clear: clearStoredToken,
})
