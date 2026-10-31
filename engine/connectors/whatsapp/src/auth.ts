import type { AuthenticationCreds, AuthenticationState, SignalDataTypeMap, SignalDataSet } from 'baileys';
import { z } from 'zod';
import { decode, decodeCreds, encode, parseKey } from './auth-codec.js';
import { BridgeFault } from './protocol.js';
import type { Peer } from './peer.js';

/** Serializes durable mutations; rejection poisons every subsequent dependent operation. */
export class ParentAuth {
  readonly state: AuthenticationState;
  private tail: Promise<void> = Promise.resolve();
  private closed = false;
  constructor(private readonly pipe: Peer, encoded: string | null, private readonly failed: () => void) {
    this.state = { creds: decodeCreds(encoded), keys: {
      get: <K extends keyof SignalDataTypeMap>(category: K, ids: string[]) => this.get(category, ids),
      set: data => this.set(data),
    } };
  }
  async barrier(): Promise<void> {
    await this.tail;
    if (this.closed) throw new BridgeFault('auth_store_failed');
  }
  close(): void { this.closed = true; }

  private enqueue(operation: () => Promise<void>): Promise<void> {
    const next = this.tail.then(async () => {
      if (this.closed) throw new BridgeFault('auth_store_failed');
      await operation();
    });
    this.tail = next;
    void next.catch(() => { this.closed = true; this.failed(); });
    return next;
  }

  save(update: Partial<AuthenticationCreds> = {}): Promise<void> {
    Object.assign(this.state.creds, update);
    const snapshot = encode(this.state.creds);
    return this.enqueue(async () => { await this.pipe.request({ type: 'creds_set', creds: snapshot }); });
  }

  private async get<K extends keyof SignalDataTypeMap>(category: K, ids: string[]): Promise<Record<string, SignalDataTypeMap[K]>> {
    await this.barrier();
    if (this.closed) throw new BridgeFault('auth_store_failed');
    try {
      const value = await this.pipe.request({ type: 'auth_get', category, ids });
      const record = z.record(z.string(), z.unknown()).parse(decode(JSON.stringify(value)));
      const result: Record<string, SignalDataTypeMap[K]> = {};
      for (const id of ids) {
        const entry = record[id];
        if (entry !== undefined && entry !== null) result[id] = parseKey(category, entry);
      }
      return result;
    } catch { // Parent-store boundary: malformed or failed reads revoke the socket as well.
      this.closed = true; this.failed(); throw new BridgeFault('auth_store_failed');
    }
  }

  private set(data: SignalDataSet): Promise<void> {
    const batches = Object.entries(data).map(([category, values]) => ({ category,
      values: z.record(z.string(), z.json()).parse(JSON.parse(encode(values))) }));
    return this.enqueue(async () => {
      if (batches.length > 0) await this.pipe.request({ type: 'auth_set', batches });
    });
  }
}
