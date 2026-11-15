import { Peer, sameToken } from './peer.js';
import { assertNever, BridgeFault } from './protocol.js';
import type { CloseReason, ParentFrame } from './protocol.js';
import { SdkAdapter } from './sdk.js';
import type { SdkClient } from './sdk.js';
import { Sends } from './sends.js';

type Host = { readonly client: () => SdkClient; readonly write: (line: string) => void; readonly exit: () => void };
export class Runtime {
  private peer: Peer | undefined;
  private adapter: SdkAdapter | undefined;
  private sends: Sends | undefined;
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
          this.loginDeadline = setTimeout(() => this.stop('login_failed'), 120_000);
          void this.login(frame.credentials);
          return;
        case 'lease': this.renew(frame.lease_ms); return;
        case 'result': this.peer.result(frame); return;
        case 'shutdown': this.stop('shutdown'); return;
        case 'send': this.sends?.receive(frame); return;
        default: assertNever(frame);
      }
    } catch (error) { this.stop(error instanceof BridgeFault ? error.reason : 'protocol_error'); }
  }
  private renew(milliseconds: number): void {
    clearTimeout(this.lease);
    this.lease = setTimeout(() => this.stop('lease_expired'), milliseconds);
  }
  private async login(credentials: string | null): Promise<void> {
    const peer = this.peer;
    if (!peer) return;
    try {
      const adapter = new SdkAdapter(this.host.client(), {
        qr: (value, expiresAt) => peer.emit({ type: 'qr', value, expires_at: expiresAt }),
        closed: reason => this.stop(reason),
      });
      this.adapter = adapter;
      this.sends = new Sends(result => peer.emit(result), command => adapter.send(command));
      await adapter.login(credentials);
      if (this.stopped) return;
      await peer.persist(adapter.credentials());
      if (this.stopped) return;
      peer.emit({ type: 'identity', account_id: adapter.accountId() });
      adapter.listen({ connected: () => {
        if (this.stopped || this.ready) return;
        this.ready = true;
        clearTimeout(this.loginDeadline);
        this.sends?.open();
        peer.emit({ type: 'ready' });
      }, message: payload => { if (this.ready && !this.stopped) peer.emit(payload); },
      closed: () => this.stop('transport_failed') });
    } catch (error) {
      this.stop(error instanceof BridgeFault ? error.reason : 'login_failed');
    }
  }
  stop(reason: CloseReason): void {
    if (this.stopped) return;
    this.stopped = true;
    this.ready = false;
    clearTimeout(this.lease); clearTimeout(this.loginDeadline);
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
