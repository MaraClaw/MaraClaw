type SessionScope = {
  readonly id: string
  readonly user_id?: string | undefined
  readonly source_channel?: string | undefined
  readonly is_primary?: boolean | undefined
  readonly is_group?: boolean | undefined
  readonly writable?: boolean | undefined
}

// The engine decides who may write (a group, a peer agent, or the caller's own conversation) and reports it per row.
export function isWritableSession(session: SessionScope | undefined): boolean {
  return session?.writable === true
}

// The caller's canonical web row with the agent: the one POST /sessions returns and the list puts first.
export function isOwnConversation(session: SessionScope, userId: string | undefined): boolean {
  return userId !== undefined && isWritableSession(session) && session.user_id === userId
    && session.source_channel === 'web' && session.is_primary === true && session.is_group !== true
}

export function controlSessionId(sessions: readonly SessionScope[], requestedId: string, userId: string | undefined): string | undefined {
  const requested = sessions.find((session) => session.id === requestedId)
  return requested && isWritableSession(requested) ? requested.id : sessions.find((session) => isOwnConversation(session, userId))?.id
}
