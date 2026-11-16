import { Frames } from '../src/protocol.js';
import type { WAMessage } from 'baileys';
import { Runtime } from '../src/runtime.js';
import { data, MediaSocket, mediaMessage } from './media-support.js';

const frames = new Frames();
class SelfSocket extends MediaSocket {
  override readonly user = { id: '15550001@s.whatsapp.net' };
  barrier: () => Promise<void> = async () => undefined;
  burst(): void {
    this.ev.emit('messages.upsert', { type: 'notify', messages: Array.from({ length: 40 }, (_, index) => ({
      key: { id: `NATIVE-TEXT-${index}`, remoteJid: this.user.id, fromMe: true },
      messageTimestamp: Math.floor(Date.now() / 1000), message: { conversation: `queued text ${index}` },
    })) });
  }
  override async *download(message: WAMessage, signal: AbortSignal): AsyncGenerator<Uint8Array> {
    if (process.argv[2] === 'burst') this.burst();
    this.ev.emit('creds.update', {});
    await this.barrier();
    yield* super.download(message, signal);
  }
}
const socket = new SelfSocket();
const runtime = new Runtime({ factory: auth => { socket.barrier = () => auth.barrier(); return socket; }, write: line => {
  const payload: unknown = JSON.parse(line);
  const malformed = process.argv[2] === 'malformed' && typeof payload === 'object' && payload !== null &&
    'type' in payload && payload.type === 'media_chunk';
  process.stdout.write(malformed ? JSON.stringify({ ...payload, data: 'invalid base64' }) + '\n' : line);
  if (typeof payload === 'object' && payload !== null && 'type' in payload) {
    if (payload.type === 'ready') queueMicrotask(() => socket.ev.emit('connection.update', { connection: 'open' }));
    if (payload.type === 'identity') queueMicrotask(() => {
      if (process.argv[2] === 'text-close') { socket.burst(); void runtime.stop('shutdown'); return; }
      const message = mediaMessage();
      if (process.argv[2] === 'wrapped' && message.message) message.message = { documentWithCaptionMessage: { message: message.message } };
      if (process.argv[2] === 'mismatch' && message.message?.documentMessage) message.message.documentMessage.fileLength = data.length + 1;
      socket.ev.emit('messages.upsert', { type: 'notify', messages: [{ ...message, key: { ...message.key, remoteJid: socket.user.id } }] });
    });
  }
}, exit: () => { process.stdin.destroy(); } });
process.stdin.on('data', (data: Buffer) => {
  for (const frame of frames.push(data)) {
    runtime.receive(frame);
    if (process.argv[2] === 'mismatch' && frame.type === 'media_error') {
      socket.ev.emit('messages.upsert', { type: 'notify', messages: [{
        key: { id: 'AFTER-MEDIA', remoteJid: socket.user.id, fromMe: true },
        messageTimestamp: Math.floor(Date.now() / 1000), message: { conversation: 'text after failed media' },
      }] });
    }
  }
});
process.stdin.on('end', () => { void runtime.stop('shutdown'); });
