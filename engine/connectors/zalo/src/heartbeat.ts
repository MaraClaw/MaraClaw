import { BridgeFault } from './protocol.js';
import type { CloseReason } from './protocol.js';

type Operations = {
  readonly ping: () => Promise<void>;
  readonly refresh: () => Promise<void>;
  readonly emit: (ok: boolean) => void;
  readonly stop: (reason: CloseReason) => void;
};

/** A new timer is armed only after the provider call and credential commit settle. */
export class Heartbeat {
  private active = true;
  private failures = 0;
  private timer: NodeJS.Timeout | undefined;
  constructor(private readonly interval: number, private readonly operations: Operations) {}
  start(): void { this.timer = setTimeout(() => { void this.run(); }, this.interval); }
  private async run(): Promise<void> {
    if (!this.active) return;
    this.timer = setTimeout(() => {
      if (!this.active) return;
      this.operations.emit(false);
      this.operations.stop('transport_failed');
    }, 15_000);
    let ok = true;
    try { await this.operations.ping(); }
    catch { ok = false; } // no-excuse-ok: catch -- provider boundary exposes only a boolean outcome.
    clearTimeout(this.timer);
    if (!this.active) return;
    try { await this.operations.refresh(); }
    catch (error) {
      this.operations.stop(error instanceof BridgeFault ? error.reason : 'auth_store_failed');
      return;
    }
    if (!this.active) return;
    this.operations.emit(ok);
    this.failures = ok ? 0 : this.failures + 1;
    if (this.failures >= 3) { this.operations.stop('transport_failed'); return; }
    this.start();
  }
  close(): void { this.active = false; clearTimeout(this.timer); }
}
