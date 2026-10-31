import { EventEmitter } from 'node:events';
import { setImmediate } from 'node:timers/promises';
import type { BaileysEventEmitter, Contact } from 'baileys';
import { z } from 'zod';
import { Peer } from '../src/peer.js';
import type { Ack, Transport } from '../src/transport.js';
import type { Json, ParentFrame, SendCommand } from '../src/protocol.js';

export const token = 'private-test-capability';
export const jid = '15551234567@s.whatsapp.net';
export const child = z.object({ type: z.string(), id: z.string(), token: z.string() }).passthrough();
export type Frame = z.infer<typeof child>;
export const settle = async (): Promise<void> => { await setImmediate(); };
export function deferred<T>(): { readonly promise: Promise<T>; readonly resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => { throw new Error('uninitialized promise'); };
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

export class ControlledTransport implements Transport {
  readonly user: Contact = { id: '15551234567:2@s.whatsapp.net', lid: '123456789:2@lid' };
  readonly sends: { readonly jid: string; readonly text: string; readonly id: string }[] = [];
  ended = 0;
  sendFailure = false;
  ackListener: ((ack: Ack) => void) | undefined;
  private readonly emitter = new EventEmitter();
  readonly ev: BaileysEventEmitter = {
    on: (event, listener) => { this.emitter.on(event, listener); },
    off: (event, listener) => { this.emitter.off(event, listener); },
    removeAllListeners: event => { this.emitter.removeAllListeners(event); },
    emit: (event, value) => this.emitter.emit(event, value),
  };
  async send(target: string, text: string, id: string): Promise<void> {
    this.sends.push({ jid: target, text, id });
    if (this.sendFailure) throw new Error('controlled transport failure');
  }
  onAck(listener: (ack: Ack) => void): () => void {
    this.ackListener = listener;
    return () => { this.ackListener = undefined; };
  }
  async end(): Promise<void> { this.ended++; }
}

export class ParentStore {
  readonly frames: Frame[] = [];
  get businessFrames(): Frame[] { return this.frames.filter(frame => frame.type !== 'diagnostic'); }
  readonly values = new Map<string, Record<string, Json>>();
  readonly peer = new Peer(token, line => this.write(line));
  automatic = true;
  accept = true;
  reply: (frame: ParentFrame) => void = frame => {
    if (frame.type !== 'result') throw new Error('expected result');
    this.peer.result(frame);
  };
  write(line: string): void {
    const raw: unknown = JSON.parse(line);
    const frame = child.parse(raw);
    this.frames.push(frame);
    if (this.automatic && ['auth_get', 'auth_set', 'creds_set'].includes(frame.type)) {
      queueMicrotask(() => this.ack(frame));
    }
  }
  ack(frame: Frame): void {
    let value: Json = null;
    switch (frame.type) {
      case 'auth_set': {
        if (this.accept) {
          const batches = z.array(z.strictObject({ category: z.string(), values: z.record(z.string(), z.json()) }))
            .parse(frame['batches']);
          for (const { category, values } of batches) {
            const stored = this.values.get(category) ?? {};
            for (const [id, entry] of Object.entries(values)) {
              if (entry === null) delete stored[id]; else stored[id] = entry;
            }
            this.values.set(category, stored);
          }
        }
        break;
      }
      case 'auth_get': {
        const category = z.string().parse(frame['category']);
        const ids = z.array(z.string()).parse(frame['ids']);
        const stored = this.values.get(category) ?? {};
        const result: Record<string, Json> = {};
        for (const id of ids) { const entry = stored[id]; if (entry !== undefined) result[id] = entry; }
        value = result;
        break;
      }
      case 'creds_set': break;
      default: throw new Error('not an RPC');
    }
    this.reply({ type: 'result', id: frame.id, token, ok: this.accept, value,
      error: this.accept ? null : 'store_failed' });
  }
}

export function command(overrides: Partial<SendCommand> = {}): SendCommand {
  return { type: 'send', id: 'correlation-1', token, command_id: 'command-1', message_id: 'RESERVED-NATIVE-ID',
    self_jid: jid, text: 'controlled text', timeout_ms: 1000, ...overrides };
}
