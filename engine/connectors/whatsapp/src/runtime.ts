import type { BaileysEventMap } from 'baileys';
import { z } from 'zod';
import { ParentAuth } from './auth.js';
import { notifyText, socketIdentity } from './input.js';
import type { Identity } from './input.js';
import { Peer } from './peer.js';
import { assertNever } from './protocol.js';
import type { ChildPayload, CloseReason, ParentFrame } from './protocol.js';
import { Sends } from './sends.js';
import type { Transport, TransportFactory } from './transport.js';

const disconnectError = z.object({ output: z.object({ statusCode: z.number().int() }) });
const reconnectMs = [1000, 2000, 4000, 8000, 16000] as const;
export type RuntimeHost = {
  readonly factory: TransportFactory;
  readonly write: (line: string) => void;
  readonly exit: () => void;
};
export class Runtime {
  private peer: Peer | undefined;
  private auth: ParentAuth | undefined;
  private socket: Transport | undefined;
  private identity: Identity | null = null;
  private connected = false;
  private stopped = false;
  private readonly closing = new WeakSet<Transport>();
  private retries = 0;
  private detach: (() => void) | undefined;
  private lease: ReturnType<typeof setTimeout> | undefined;
  private reconnect: ReturnType<typeof setTimeout> | undefined;
  private pairTimer: ReturnType<typeof setTimeout> | undefined;
  private readonly sends: Sends;
  private pairDeadline = 0;
  constructor(private readonly host: RuntimeHost) {
    this.sends = new Sends({ emit: payload => this.emit(payload),
      barrier: async () => { await this.auth?.barrier(); },
      current: () => this.connected && this.socket && this.identity ? { socket: this.socket, identity: this.identity } : null,
      failed: () => { void this.stop('transport_failed'); },
    });
  }

  receive(frame: ParentFrame): void {
    if (this.stopped) return;
    if (this.peer) this.peer.authenticate(frame.token);
    else if (frame.type !== 'init') { void this.stop('transport_failed'); return; }
    switch (frame.type) {
      case 'init':
        if (this.peer) { void this.stop('transport_failed'); return; }
        this.peer = new Peer(frame.token, this.host.write);
        this.renew(frame.lease_ms);
        void this.initialize(frame.creds).catch(() => this.stop('auth_store_failed'));
        return;
      case 'lease': this.renew(frame.lease_ms); return;
      case 'send': this.sends.submit(frame); return;
      case 'shutdown': void this.stop('shutdown'); return;
      case 'result': this.peer?.result(frame); return;
      default: assertNever(frame);
    }
  }

  private async initialize(creds: string | null): Promise<void> {
    if (!this.peer) return;
    this.auth = new ParentAuth(this.peer, creds, () => { void this.stop('auth_store_failed'); });
    if (!this.auth.state.creds.registered) {
      this.pairDeadline = Date.now() + 120_000;
      this.pairTimer = setTimeout(() => { void this.stop('transport_failed'); }, 120_000);
    }
    await this.auth.save();
    if (!this.stopped) this.open();
  }

  private renew(ms: number): void {
    clearTimeout(this.lease);
    this.lease = setTimeout(() => { void this.stop('lease_expired'); }, ms);
  }

  private open(): void {
    if (this.stopped || !this.auth) return;
    const socket = this.host.factory(this.auth);
    this.socket = socket;
    this.detach = socket.onAck(ack => { if (!this.stopped && this.socket === socket) this.sends.ack(socket, ack); });
    socket.ev.on('creds.update', update => {
      if (!this.stopped && this.socket === socket) void this.auth?.save(update);
    });
    socket.ev.on('connection.update', update => {
      if (!this.stopped && this.socket === socket) {
        void this.connection(socket, update).catch(() => this.stop('auth_store_failed'));
      }
    });
    socket.ev.on('messages.upsert', event => {
      if (this.stopped || this.socket !== socket || !this.connected) return;
      for (const payload of notifyText(event, this.identity)) this.emit(payload);
    });
    socket.ev.on('messages.update', updates => { if (!this.stopped) this.sends.updates(socket, updates); });
    this.emit({ type: 'ready' });
  }

  private async connection(socket: Transport, update: BaileysEventMap['connection.update']): Promise<void> {
    // Revoke intake synchronously before awaiting credential commits from this socket.
    if (this.closing.has(socket)) return;
    if (update.connection === 'close') {
      this.closing.add(socket); this.connected = false; this.identity = null; this.sends.disconnect(socket);
    }
    await this.auth?.barrier();
    if (this.stopped || this.socket !== socket) return;
    if (update.qr && this.pairDeadline > Date.now()) this.emit({ type: 'qr', value: update.qr,
      expires_at: new Date(Math.min(this.pairDeadline, Date.now() + 20_000)).toISOString() });
    switch (update.connection) {
      case 'open': {
        if (this.closing.has(socket)) return;
        const identity = socketIdentity(socket.user);
        if (!identity) { await this.stop('transport_failed'); return; }
        this.identity = identity;
        this.connected = true;
        clearTimeout(this.pairTimer);
        this.emit(identity);
        return;
      }
      case 'close': {
        const parsed = disconnectError.safeParse(update.lastDisconnect?.error);
        const code = parsed.success ? parsed.data.output.statusCode : 0;
        switch (code) {
          case 401: await this.stop('logged_out'); return;
          case 440: await this.stop('replaced'); return;
          case 403: await this.stop('forbidden'); return;
          default: await this.reopen(socket);
        }
        return;
      }
      case 'connecting': case undefined: return;
      default: assertNever(update.connection);
    }
  }

  private async release(socket: Transport): Promise<void> {
    this.detach?.(); this.detach = undefined;
    for (const event of ['creds.update', 'connection.update', 'messages.upsert', 'messages.update'] as const) {
      socket.ev.removeAllListeners(event);
    }
    await socket.end();
  }

  private async reopen(socket: Transport): Promise<void> {
    this.socket = undefined;
    const timeout = setTimeout(() => { void this.stop('transport_failed'); }, 2000);
    await this.release(socket);
    clearTimeout(timeout);
    if (this.stopped) return;
    const delay = reconnectMs[this.retries++];
    if (delay === undefined) { await this.stop('transport_failed'); return; }
    this.reconnect = setTimeout(() => {
      void this.auth?.barrier().then(() => this.open()).catch(() => this.stop('auth_store_failed'));
    }, delay);
  }

  private emit(payload: ChildPayload): void {
    try { this.peer?.emit(payload); } catch { void this.stop('transport_failed'); }
  }

  async stop(reason: CloseReason): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    this.connected = false;
    clearTimeout(this.lease); clearTimeout(this.reconnect); clearTimeout(this.pairTimer);
    this.auth?.close();
    this.sends.close();
    const force = setTimeout(() => this.finish(reason), 2000);
    try { if (this.socket) await this.release(this.socket); }
    catch { this.finish('transport_failed'); }
    clearTimeout(force);
    this.finish(reason);
  }
  private finished = false;
  private finish(reason: CloseReason): void {
    if (this.finished) return;
    this.finished = true;
    this.emit({ type: 'closed', reason });
    this.peer?.close();
    this.host.exit();
  }
}
