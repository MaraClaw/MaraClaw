import makeWASocket, { downloadMediaMessage } from 'baileys';
import type { BaileysEventEmitter, Contact, WAMessage } from 'baileys';
import { pino } from 'pino';
import { z } from 'zod';
import type { ParentAuth } from './auth.js';
import { BridgeFault } from './protocol.js';

export type Ack = { readonly id: string; readonly jid: string | null; readonly accepted: boolean };
export interface Transport {
  readonly ev: BaileysEventEmitter;
  readonly user: Contact | undefined;
  send(jid: string, text: string, id: string): Promise<void>;
  onAck(listener: (ack: Ack) => void): () => void;
  download(message: WAMessage, signal: AbortSignal): AsyncGenerator<Uint8Array>;
  end(): Promise<void>;
}
export type TransportFactory = (auth: ParentAuth) => Transport;
const ackNode = z.object({ tag: z.literal('ack'), attrs: z.object({
  class: z.literal('message'), id: z.string(), from: z.string().optional(), error: z.string().optional(),
}).passthrough() }).passthrough();

export const createTransport: TransportFactory = auth => {
  const socket = makeWASocket({
    auth: auth.state, logger: pino({ level: 'silent' }),
    syncFullHistory: false, shouldSyncHistoryMessage: () => false,
    markOnlineOnConnect: false, emitOwnEvents: false,
    enableAutoSessionRecreation: false, enableRecentMessageCache: false,
    maxMsgRetryCount: 0, retryRequestDelayMs: 60_000,
    connectTimeoutMs: 20_000, defaultQueryTimeoutMs: 20_000,
    transactionOpts: { maxCommitRetries: 0, delayBetweenTriesMs: 0 },
    getMessage: async () => undefined,
  });
  const send = socket.ws.send.bind(socket.ws);
  // Baileys does not await creds.update listeners. Gate the actual wire, not just sendMessage.
  socket.ws.send = (data, callback) => {
    void auth.barrier().then(() => {
      if (socket.ws.isClosing || socket.ws.isClosed) { callback?.(new BridgeFault()); return; }
      send(data, callback);
    }).catch(() => callback?.(new BridgeFault('auth_store_failed')));
    return true;
  };
  return {
    ev: socket.ev,
    get user() { return socket.user; },
    send: async (jid, text, id) => {
      await socket.sendMessage(jid, { text, linkPreview: null }, { messageId: id });
    },
    onAck: listener => {
      const handler = (node: unknown): void => {
        const parsed = ackNode.safeParse(node);
        if (parsed.success) listener({ id: parsed.data.attrs.id, jid: parsed.data.attrs.from ?? null,
          accepted: parsed.data.attrs.error === undefined });
      };
      socket.ws.on('CB:ack,class:message', handler);
      return () => { socket.ws.off('CB:ack,class:message', handler); };
    },
    download: async function* (message, signal) {
      await auth.barrier();
      signal.throwIfAborted();
      const stream = await downloadMediaMessage(message, 'stream', { options: { signal } });
      try {
        for await (const chunk of stream) {
          if (!(chunk instanceof Uint8Array)) throw new BridgeFault();
          yield chunk;
        }
      } finally { stream.destroy(); }
    },
    end: async () => { await socket.end(undefined); },
  };
};
