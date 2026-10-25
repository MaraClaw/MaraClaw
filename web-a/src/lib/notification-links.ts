const adminNotificationRoute = /^\/(?:|account|settings|notifications|admission-approvals(?:\/[a-zA-Z0-9_-]+)?|users(?:\/[a-zA-Z0-9_-]+)?|companies(?:\/[a-zA-Z0-9_-]+)?|search-engine|broadcast|models|tools)(?:[?#][^\s\\]*)?$/

export function safeNotificationLink(link: string | null): string | null {
  if (link?.startsWith('/agent-deletion-requests')) {
    return /^\/agent-deletion-requests(?:\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?$/i.test(link) ? link : null
  }
  return link !== null && adminNotificationRoute.test(link) &&
    Array.from(link).every((character) => character.charCodeAt(0) > 32 && character.charCodeAt(0) !== 127)
    ? link
    : null
}
