import { randomUUID, timingSafeEqual } from "node:crypto";
import { BridgeFault, MAX_FRAME_BYTES } from "./protocol.js";
import type { ChildFrame, ChildPayload, Json, ParentFrame } from "./protocol.js";
import { diagnostic, safeCategory } from "./diagnostics.js";

type Pending = { readonly resolve: (value: Json) => void; readonly reject: (error: BridgeFault) => void; readonly onClose: () => void; readonly timer: NodeJS.Timeout };
export class Peer {
  private readonly pending = new Map<string, Pending>();
  private active = true;
  constructor(readonly token: string, private readonly write: (line: string) => void) {}
  authenticate(token: string): void {
    const expected = Buffer.from(this.token);
    const received = Buffer.from(token);
    if (received.length !== expected.length || !timingSafeEqual(expected, received)) throw new BridgeFault();
  }
  emit(payload: ChildPayload, id = randomUUID()): void {
    if (!this.active) throw new BridgeFault();
    const frame: ChildFrame = { ...payload, id, token: this.token };
    const line = JSON.stringify(frame);
    if (Buffer.byteLength(line) > MAX_FRAME_BYTES) throw new BridgeFault();
    this.write(`${line}\n`);
  }
  request(payload: Extract<ChildPayload, { type: "auth_get" | "auth_set" | "creds_set" }>): Promise<Json> {
    if (this.pending.size >= 128 || !this.active) return Promise.reject(new BridgeFault("auth_store_failed"));
    const id = randomUUID();
    const started = performance.now();
    const category = payload.type === 'auth_get' ? safeCategory(payload.category) : payload.type === 'creds_set' ? 'creds' : 'other';
    const count = payload.type === 'auth_get' ? payload.ids.length : payload.type === 'creds_set' ? 1 : payload.batches.reduce((n, batch) => n + Object.keys(batch.values).length, 0);
    const categories = payload.type === 'auth_set' ? [...new Set(payload.batches.map(batch => safeCategory(batch.category)))] : [];
    const context = { type: 'diagnostic', stage: 'auth_rpc', operation: payload.type, category, categories, count, rpc_id: id } as const;
    diagnostic(this, { ...context, event: 'start' });
    return new Promise<Json>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        diagnostic(this, { ...context, event: 'failure', error_kind: 'timeout', elapsed_ms: Math.round(performance.now() - started) });
        reject(new BridgeFault("auth_store_failed"));
      }, 15_000);
      this.pending.set(id, { resolve: value => { diagnostic(this, { ...context, event: 'committed', elapsed_ms: Math.round(performance.now() - started) }); resolve(value); },
        reject: error => { diagnostic(this, { ...context, event: 'failure', error_kind: this.active ? 'parent_rejected' : 'closed', elapsed_ms: Math.round(performance.now() - started) }); reject(error); },
        onClose: () => { diagnostic(this, { ...context, event: 'failure', error_kind: 'closed', elapsed_ms: Math.round(performance.now() - started) }); }, timer });
      try { this.emit(payload, id); }
      catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        diagnostic(this, { ...context, event: 'failure', error_kind: 'other', elapsed_ms: Math.round(performance.now() - started) });
        reject(error instanceof BridgeFault ? error : new BridgeFault("auth_store_failed"));
      }
    });
  }
  result(frame: Extract<ParentFrame, { type: "result" }>): void {
    const pending = this.pending.get(frame.id);
    if (!pending) throw new BridgeFault();
    this.pending.delete(frame.id);
    clearTimeout(pending.timer);
    if (frame.ok && frame.error === null) pending.resolve(frame.value);
    else pending.reject(new BridgeFault("auth_store_failed"));
  }
  close(): void {
    for (const pending of this.pending.values()) pending.onClose();
    this.active = false;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new BridgeFault("auth_store_failed"));
    }
    this.pending.clear();
  }
}
