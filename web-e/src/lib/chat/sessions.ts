type SessionScope = {
  readonly id: string
  readonly source_channel?: string | undefined
  readonly is_group?: boolean | undefined
}

export function isWritableSession(session: SessionScope | undefined): boolean {
  return session !== undefined && (session.source_channel === 'shared' || session.source_channel === 'agent' || session.is_group === true)
}

export function controlSessionId(sessions: readonly SessionScope[], requestedId: string): string | undefined {
  const requested = sessions.find((session) => session.id === requestedId)
  return requested && isWritableSession(requested) ? requested.id : sessions.find((session) => session.source_channel === 'shared')?.id
}
