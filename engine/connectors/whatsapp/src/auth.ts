import type { AuthenticationCreds, AuthenticationState, SignalDataTypeMap, SignalDataSet } from 'baileys';
import { z } from 'zod';
import { decode, decodeCreds, encode, parseKey } from './auth-codec.js';
import { BridgeFault } from './protocol.js';
import type { AuthBatch } from './protocol.js';
import type { Peer } from './peer.js';
import { diagnostic, failure, safeCategory } from './diagnostics.js';

/** Serializes durable mutations; rejection poisons every subsequent dependent operation. */
export class ParentAuth {
  readonly state: AuthenticationState;
  private tail: Promise<void> = Promise.resolve();
  private closed = false;
  constructor(private readonly pipe: Peer, encoded: string | null, private readonly failed: () => void) {
    let creds: AuthenticationCreds;
    try { creds = decodeCreds(encoded); }
    catch (error) { diagnostic(pipe, { type: 'diagnostic', stage: 'auth_initial_decode', event: 'failure', ...failure(error) }); throw error; }
    this.state = { creds, keys: {
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
    let snapshot: string;
    try { snapshot = encode(this.state.creds); }
    catch (error) { diagnostic(this.pipe, { type: 'diagnostic', stage: 'auth_write_encode', event: 'failure', operation: 'creds_set', category: 'creds', ...failure(error) }); throw error; }
    return this.enqueue(async () => { await this.pipe.request({ type: 'creds_set', creds: snapshot }); });
  }

  private async get<K extends keyof SignalDataTypeMap>(category: K, ids: string[]): Promise<Record<string, SignalDataTypeMap[K]>> {
    await this.barrier();
    if (this.closed) throw new BridgeFault('auth_store_failed');
    try {
      const value = await this.pipe.request({ type: 'auth_get', category, ids });
      let record: Record<string, unknown>;
      try { record = z.record(z.string(), z.unknown()).parse(decode(JSON.stringify(value))); }
      catch (error) { diagnostic(this.pipe, { type: 'diagnostic', stage: 'auth_read', event: 'failure', operation: 'auth_get', category: safeCategory(category), count: ids.length, ...failure(error, value) }); throw error; }
      const result: Record<string, SignalDataTypeMap[K]> = {};
      for (const id of ids) {
        const entry = record[id];
        if (entry !== undefined && entry !== null) {
          try { result[id] = parseKey(category, entry); }
          catch (error) { diagnostic(this.pipe, { type: 'diagnostic', stage: 'auth_key_decode', event: 'failure', operation: 'auth_get', category: safeCategory(category), count: ids.length, ...failure(error, entry) }); throw error; }
        }
      }
      return result;
    } catch { // Parent-store boundary: malformed or failed reads revoke the socket as well.
      this.closed = true; this.failed(); throw new BridgeFault('auth_store_failed');
    }
  }

  private set(data: SignalDataSet): Promise<void> {
    const batches: AuthBatch[] = Object.entries(data).map(([category, values]) => {
      try { return { category, values: z.record(z.string(), z.json()).parse(JSON.parse(encode(values))) }; }
      catch (error) { diagnostic(this.pipe, { type: 'diagnostic', stage: 'auth_write_encode', event: 'failure',
        operation: 'auth_set', category: safeCategory(category), ...failure(error, values) }); throw error; }
    });
    return this.enqueue(async () => {
      if (batches.length > 0) await this.pipe.request({ type: 'auth_set', batches });
    });
  }
}
