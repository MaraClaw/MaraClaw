import { randomUUID, timingSafeEqual } from 'node:crypto';
import { BridgeFault, childFrame, MAX_FRAME_BYTES } from './protocol.js';
import type { ChildPayload, ParentFrame } from './protocol.js';

export function sameToken(expected: string, received: string): boolean {
  const left = Buffer.from(expected);
  const right = Buffer.from(received);
  return left.length === right.length && timingSafeEqual(left, right);
}

type Commit = { readonly id: string; readonly timer: NodeJS.Timeout; readonly resolve: () => void;
  readonly reject: (error: BridgeFault) => void };
export class Peer {
  private commit: Commit | undefined;
  constructor(readonly token: string, private readonly write: (line: string) => void) {}
  emit(payload: ChildPayload, id: string = randomUUID()): void {
    const frame = childFrame.parse({ ...payload, id, token: this.token });
    const line = JSON.stringify(frame);
    if (Buffer.byteLength(line) > MAX_FRAME_BYTES) throw new BridgeFault('protocol_error');
    this.write(`${line}\n`);
  }
  persist(creds: string): Promise<void> {
    if (this.commit) throw new BridgeFault('auth_store_failed');
    return new Promise<void>((resolve, reject) => {
      const id = randomUUID();
      const timer = setTimeout(() => this.cancel(), 15_000);
      this.commit = { id, timer, resolve, reject };
      this.emit({ type: 'creds_set', creds }, id);
    });
  }
  result(frame: Extract<ParentFrame, { type: 'result' }>): void {
    const commit = this.commit;
    if (!commit || commit.id !== frame.request_id) throw new BridgeFault('protocol_error');
    clearTimeout(commit.timer);
    this.commit = undefined;
    if (frame.ok) commit.resolve();
    else commit.reject(new BridgeFault('auth_store_failed'));
  }
  cancel(): void {
    const commit = this.commit;
    this.commit = undefined;
    if (commit) {
      clearTimeout(commit.timer);
      commit.reject(new BridgeFault('auth_store_failed'));
    }
  }
}
