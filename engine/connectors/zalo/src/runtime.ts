import { Peer, sameToken } from './peer.js';
import { assertNever, BridgeFault } from './protocol.js';
import type { CloseReason, ParentFrame } from './protocol.js';
import { SdkAdapter } from './sdk.js';
import type { SdkClient } from './sdk.js';
import { Sends } from './sends.js';
import { Heartbeat } from './heartbeat.js';
import { Typing } from './typing.js';
import { MediaTransport } from './media_transport.js';

type Host = { readonly client: () => SdkClient; readonly write: (line: string) => void; readonly exit: () => void };
export class Runtime {
  private peer: Peer | undefined;
  private adapter: SdkAdapter | undefined;
  private sends: Sends | undefined;
  private heartbeat: Heartbeat | undefined;
  private typing: Typing | undefined;
  private media: MediaTransport | undefined;
  private committing = false;
  private committed: string | undefined;
  private stopped = false;
  private ready = false;
  private lease: NodeJS.Timeout | undefined;
  private loginDeadline: NodeJS.Timeout | undefined;
  constructor(private readonly host: Host) {}
  receive(frame: ParentFrame): void {
    if (this.stopped) return;
    try {
      if (!this.peer) {
        if (frame.type !== 'init') throw new BridgeFault('protocol_error');
        this.peer = new Peer(frame.token, this.host.write);
      } else if (frame.type === 'init' || !sameToken(this.peer.token, frame.token)) {
        throw new BridgeFault('protocol_error');
      }
      switch (frame.type) {
        case 'init':
          this.renew(frame.lease_ms);
          this.loginDeadline = setTimeout(() => this.stop(frame.credentials === null ? 'login_failed' : 'transport_failed'), 120_000);
           void this.login(frame.credentials, frame.keepalive_interval_ms ?? 300_000, frame.media_allowed_hosts ?? []);
          return;
        case 'lease': this.renew(frame.lease_ms); return;
        case 'result': this.peer.result(frame); return;
        case 'shutdown': this.stop('shutdown'); return;
        case 'send': this.sends?.receive(frame); return;
        case 'typing': if (this.ready && !this.committing) this.typing?.receive(frame.thread_id); return;
        case 'media_get': case 'media_put': case 'media_chunk': case 'media_end': case 'media_ack': case 'media_error':
          this.media?.receive(frame); return;
        default: assertNever(frame);
      }
    } catch (error) { this.stop(error instanceof BridgeFault ? error.reason : 'protocol_error'); }
  }
  private renew(milliseconds: number): void {
    clearTimeout(this.lease);
    this.lease = setTimeout(() => this.stop('lease_expired'), milliseconds);
  }
  private async login(credentials: string | null, interval: number, hosts: readonly string[]): Promise<void> {
    const peer = this.peer;
    if (!peer) return;
    try {
      const adapter = new SdkAdapter(this.host.client(), {
        qr: (value, expiresAt) => peer.emit({ type: 'qr', value, expires_at: expiresAt }),
        closed: reason => this.stop(reason),
      });
      this.adapter = adapter;
      const media = new MediaTransport({ emit: payload => peer.emit(payload), cookies: () => adapter.cookies(), hosts,
        allowed: () => this.ready && !this.stopped && !this.committing });
      this.media = media;
      this.sends = new Sends(result => peer.emit(result), async command => {
        const data = media.take(command);
        try { return await adapter.send(command, data); }
        finally { media.release(command); }
      });
      this.typing = new Typing(threadId => adapter.typing(threadId));
      this.heartbeat = new Heartbeat(interval, { ping: () => adapter.keepAlive(), refresh: () => this.refresh(adapter, peer),
        emit: ok => peer.emit({ type: 'heartbeat', ok }), stop: reason => this.stop(reason) });
      await adapter.login(credentials);
      if (this.stopped) return;
      await this.refresh(adapter, peer);
      if (this.stopped) return;
      peer.emit({ type: 'identity', account_id: adapter.accountId() });
      adapter.listen({ connected: () => {
        if (this.stopped || this.ready) return;
        this.ready = true;
        clearTimeout(this.loginDeadline);
        this.sends?.open();
        peer.emit({ type: 'ready' });
        this.heartbeat?.start();
      }, message: payload => { if (this.ready && !this.stopped && !this.committing) peer.emit(payload); },
      closed: reason => this.stop(reason) }, media.offers);
    } catch (error) {
      this.stop(error instanceof BridgeFault ? error.reason : credentials === null ? 'login_failed' : 'transport_failed');
    }
  }
  private async refresh(adapter: SdkAdapter, peer: Peer): Promise<void> {
    this.committing = true;
    this.sends?.pause();
    try {
      // Snapshot to a detached string, then re-read after ACK: in-flight requests can mutate the live jar.
      while (!this.stopped) {
        const snapshot = adapter.credentials();
        if (snapshot === this.committed) break;
        await peer.persist(snapshot);
        if (this.stopped) return;
        this.committed = snapshot;
      }
      this.committing = false;
      if (this.ready && !this.stopped) this.sends?.open();
    } catch (error) {
      if (error instanceof BridgeFault) throw error;
      throw new BridgeFault('auth_store_failed');
    }
  }
  stop(reason: CloseReason): void {
    if (this.stopped) return;
    this.stopped = true;
    this.ready = false;
    clearTimeout(this.lease); clearTimeout(this.loginDeadline);
    this.heartbeat?.close(); this.typing?.close(); this.media?.close();
    try {
      this.sends?.close();
      this.peer?.cancel();
      this.adapter?.close();
    } finally {
      try { this.peer?.emit({ type: 'closed', reason }); }
      finally { this.host.exit(); }
    }
  }
}
