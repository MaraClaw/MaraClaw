import { randomUUID, timingSafeEqual } from "node:crypto";
import { BridgeFault, MAX_FRAME_BYTES } from "./protocol.js";
import type { ChildFrame, ChildPayload, Json, ParentFrame } from "./protocol.js";

type Pending = { readonly resolve: (value: Json) => void; readonly reject: (error: BridgeFault) => void; readonly timer: NodeJS.Timeout };
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
    return new Promise<Json>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new BridgeFault("auth_store_failed"));
      }, 15_000);
      this.pending.set(id, { resolve, reject, timer });
      try { this.emit(payload, id); }
      catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
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
    this.active = false;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new BridgeFault("auth_store_failed"));
    }
    this.pending.clear();
  }
}
