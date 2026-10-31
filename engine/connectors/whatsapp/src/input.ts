import { jidNormalizedUser } from 'baileys';
import type { BaileysEventMap, Contact } from 'baileys';
import type { ChildPayload } from './protocol.js';

export type Identity = Extract<ChildPayload, { type: 'identity' }>;
export function normalizeJid(value: string | null | undefined): string | null {
  if (!value || !/^\d+(?::\d+)?@(s\.whatsapp\.net|lid)$/.test(value)) return null;
  return jidNormalizedUser(value);
}
export function socketIdentity(user: Contact | undefined): Identity | null {
  if (!user) return null;
  const ids = [user.id, user.lid, user.phoneNumber].map(normalizeJid).filter(value => value !== null);
  const pns = new Set(ids.filter(id => id.endsWith('@s.whatsapp.net')));
  const lids = new Set(ids.filter(id => id.endsWith('@lid')));
  if (pns.size > 1 || lids.size > 1) return null;
  const pn = pns.values().next().value ?? null;
  const lid = lids.values().next().value ?? null;
  return pn === null ? null : { type: 'identity', account_id: pn, pn_jid: pn, lid_jid: lid };
}
export function selfJid(jid: string | null, identity: Identity | null): boolean {
  return jid !== null && identity !== null && (jid === identity.pn_jid || jid === identity.lid_jid);
}
export function notifyText(event: BaileysEventMap['messages.upsert'], identity: Identity | null): ChildPayload[] {
  if (event.type !== 'notify' || event.requestId !== undefined || identity === null) return [];
  const result: ChildPayload[] = [];
  for (const message of event.messages) {
    const remote = normalizeJid(message.key.remoteJid);
    const participant = normalizeJid(message.key.participant);
    if (!selfJid(remote, identity) || (message.key.participant && !selfJid(participant, identity))) continue;
    const content = message.message;
    if (!content || !message.key.id || message.messageStubType != null) continue;
    const fields = Object.keys(content).filter(key => key !== 'messageContextInfo');
    if (fields.length !== 1) continue;
    const text = content.conversation ?? content.extendedTextMessage?.text;
    if (typeof text !== 'string' || text.length === 0 || Array.from(text).length > 4096) continue;
    const context = content.extendedTextMessage?.contextInfo;
    if (context?.quotedMessage || context?.stanzaId || context?.isForwarded || context?.forwardingScore) continue;
    const time = message.messageTimestamp;
    const timestamp = typeof time === 'number' ? time : time?.toNumber();
    if (timestamp === undefined || !Number.isSafeInteger(timestamp) || timestamp < 0) continue;
    result.push({ type: 'message', message_id: message.key.id, remote_jid: remote ?? '',
      participant, from_me: message.key.fromMe === true, timestamp, text, event_kind: 'notify', is_history: false });
  }
  return result;
}
