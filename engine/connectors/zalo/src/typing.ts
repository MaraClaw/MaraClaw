/** Best-effort pulses have no SDK stop API. Only actual settlement releases capacity. */
export class Typing {
  private active = true;
  private unsettled = 0;
  private readonly peers = new Map<string, number>();
  constructor(private readonly dispatch: (threadId: string) => Promise<void>) {}
  receive(threadId: string): void {
    if (!this.active || this.unsettled >= 4) return;
    const now = Date.now();
    const previous = this.peers.get(threadId);
    if (previous !== undefined && now - previous < 4000) return;
    if (previous === undefined && this.peers.size >= 1000) {
      for (const [peer, at] of this.peers) if (now - at >= 4000) this.peers.delete(peer);
      if (this.peers.size >= 1000) return;
    }
    this.peers.set(threadId, now);
    this.unsettled++;
    void this.dispatch(threadId).then(
      () => { if (this.active) this.unsettled--; },
      () => { if (this.active) this.unsettled--; }, // Failure is contained; no event/error content crosses the wire.
    );
  }
  close(): void { this.active = false; }
}
