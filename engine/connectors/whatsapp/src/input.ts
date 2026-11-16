import { jidNormalizedUser } from 'baileys';
import type { BaileysEventMap, Contact } from 'baileys';
import type { DropReason } from './diagnostics.js';
import type { ChildPayload } from './protocol.js';
import type { MediaOffers } from './media_offers.js';
import { originalContent } from './media_offers.js';

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
export function notifyText(event: BaileysEventMap['messages.upsert'], identity: Identity | null,
  drop?: (reason: DropReason) => void, offers?: MediaOffers): ChildPayload[] {
  if (event.type !== 'notify') { drop?.('not_notify'); return []; }
  if (identity === null) { drop?.('identity_unavailable'); return []; }
  const result: ChildPayload[] = [];
  for (const message of event.messages) {
    const remote = normalizeJid(message.key.remoteJid);
    const participant = normalizeJid(message.key.participant);
    if (!selfJid(remote, identity) || message.key.participant) { drop?.('non_self'); continue; }
    const content = originalContent(message.message);
    if (!content || !message.key.id) { drop?.('missing_content'); continue; }
    if (message.messageStubType != null) { drop?.('stub'); continue; }
    const fields = Object.keys(content).filter(key => key !== 'messageContextInfo');
    if (fields.length !== 1) { drop?.('content_shape'); continue; }
    const attachment = content.imageMessage ?? content.documentMessage ?? content.videoMessage;
    const text = content.conversation ?? content.extendedTextMessage?.text ?? attachment?.caption ?? '';
    if (Array.from(text).length > 4096 || (!attachment && text.length === 0)) { drop?.('text'); continue; }
    const context = content.extendedTextMessage?.contextInfo ?? attachment?.contextInfo;
    if (context?.quotedMessage || context?.stanzaId || context?.isForwarded || context?.forwardingScore) { drop?.('quoted_or_forwarded'); continue; }
    const time = message.messageTimestamp;
    const timestamp = typeof time === 'number' ? time : time?.toNumber();
    if (timestamp === undefined || !Number.isSafeInteger(timestamp) || timestamp < 0) { drop?.('timestamp'); continue; }
    if (attachment && message.key.fromMe !== true) { drop?.('non_self'); continue; }
    const media = attachment ? offers?.offer(message) : undefined;
    if (attachment && !media) { drop?.('media'); continue; }
    result.push({ type: 'message', message_id: message.key.id, remote_jid: remote ?? '',
      participant, from_me: message.key.fromMe === true, timestamp, text, event_kind: 'notify', is_history: false,
      ...(media ? { media } : {}) });
  }
  return result;
}
