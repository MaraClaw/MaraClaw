import { addTransactionCapability, initAuthCreds, makeEventBuffer, proto } from 'baileys';
import type { BaileysEventMap, CacheStore, SignalKeyStore, WAMessage } from 'baileys';
import { makeLibSignalRepository } from 'baileys/lib/Signal/libsignal.js';
import processMessage from 'baileys/lib/Utils/process-message.js';
import pino from 'pino';
import { jid } from './support.js';

export type Upsert = BaileysEventMap['messages.upsert'];
export const timestamp = 1_790_000_000;
const emptyKeys: SignalKeyStore = { get: () => ({}), set: () => undefined };
const emptyCache: CacheStore = { get: () => undefined, set: () => undefined, del: () => undefined, flushAll: () => undefined };

/** A self-chat WebMessageInfo as the phone would encode it. */
export function selfMessage(id: string, content: proto.IMessage, overrides: Partial<WAMessage> = {}): WAMessage {
  return { key: { remoteJid: jid, fromMe: true, id }, messageTimestamp: timestamp, message: content, ...overrides };
}

/** The actual SDK producer (processMessage) wired to the actual SDK event buffer, with inert stores. */
export function sdkHarness() {
  const logger = pino({ level: 'silent' });
  const ev = makeEventBuffer(logger);
  const keyStore = addTransactionCapability(emptyKeys, logger, { maxCommitRetries: 1, delayBetweenTriesMs: 1 });
  const creds = { ...initAuthCreds(), me: { id: '15551234567:2@s.whatsapp.net', lid: '123456789:2@lid' } };
  const context = { shouldProcessHistoryMsg: false, placeholderResendCache: emptyCache, creds, keyStore, ev, logger,
    options: {}, signalRepository: makeLibSignalRepository({ creds, keys: keyStore }, logger), getMessage: async () => undefined };
  const seen: Upsert[] = [];
  ev.on('messages.upsert', event => { seen.push(event); });
  /** Delivers an ordinary live message, which the SDK emits without a request ID. */
  const live = (message: WAMessage, type: Upsert['type'] = 'notify'): void => {
    ev.emit('messages.upsert', { messages: [message], type });
  };
  /** Delivers a phone placeholder-resend response so the SDK itself emits the recovered notify with a request ID. */
  const recover = async (message: WAMessage, requestId: string): Promise<void> => {
    const outer: WAMessage = { key: { remoteJid: jid, fromMe: true, id: `PDO-${requestId}` }, messageTimestamp: timestamp,
      message: { protocolMessage: { type: proto.Message.ProtocolMessage.Type.PEER_DATA_OPERATION_REQUEST_RESPONSE_MESSAGE,
        peerDataOperationRequestResponseMessage: { stanzaId: requestId,
          peerDataOperationRequestType: proto.Message.PeerDataOperationRequestType.PLACEHOLDER_MESSAGE_RESEND,
          peerDataOperationResult: [{ placeholderMessageResendResponse: {
            webMessageInfoBytes: proto.WebMessageInfo.encode(message).finish() } }] } } } };
    await processMessage(outer, context);
  };
  return { ev, seen, live, recover };
}
