import { createHash } from 'node:crypto';
import type { ChildPayload, ParentFrame } from './protocol.js';
import { assertNever } from './protocol.js';
import { CHUNK_BYTES, FILE_BYTES, IMAGE_BYTES, MEDIA_TIMEOUT_MS, MediaFault } from './media_protocol.js';
import { MediaOffers } from './media_offers.js';
import type { Transport } from './transport.js';

type Command = Extract<ParentFrame, { type: 'media_get' | 'media_ack' | 'media_error' }>;
type Download = { readonly id: string; readonly controller: AbortController; readonly timer: NodeJS.Timeout;
  ack: { readonly seq: number; readonly resolve: () => void; readonly reject: (error: MediaFault) => void } | undefined };
type Host = { readonly emit: (payload: ChildPayload) => void; readonly current: () => Transport | undefined;
  readonly barrier: () => Promise<void> };

/** A single transfer and one outstanding chunk; backpressure reaches the provider stream. */
export class MediaTransport {
  readonly offers = new MediaOffers();
  private download: Download | undefined;
  constructor(private readonly host: Host) {}
  receive(frame: Command): void {
    try {
      switch (frame.type) {
        case 'media_error':
          if (this.download?.id === frame.transfer_id) this.remove();
          return;
        case 'media_ack': {
          const state = this.download;
          if (!state || state.id !== frame.transfer_id || !state.ack || state.ack.seq !== frame.seq) throw new MediaFault('invalid');
          const ack = state.ack;
          state.ack = undefined;
          ack.resolve();
          return;
        }
        case 'media_get': {
          const socket = this.host.current();
          if (!socket) throw new MediaFault('forbidden');
          if (this.download) throw new MediaFault('invalid');
          const offer = this.offers.take(frame.message_id, frame.source_id);
          const limit = Math.min(frame.limit, offer.metadata.kind === 'image' ? IMAGE_BYTES : FILE_BYTES);
          if (offer.metadata.size > limit) throw new MediaFault('too_large');
          const controller = new AbortController();
          const state: Download = { id: frame.transfer_id, controller, ack: undefined,
            timer: setTimeout(() => this.error(frame.transfer_id, new MediaFault('expired')), MEDIA_TIMEOUT_MS) };
          this.download = state;
          void this.get(state, socket, offer.message, limit);
          return;
        }
        default: assertNever(frame);
      }
    } catch (error) { this.error(frame.transfer_id, error); }
  }
  private async get(state: Download, socket: Transport, message: Parameters<Transport['download']>[0], limit: number): Promise<void> {
    try {
      await this.host.barrier();
      state.controller.signal.throwIfAborted();
      if (this.host.current() !== socket) throw new MediaFault('forbidden');
      let size = 0, seq = 0;
      const hash = createHash('sha256');
      for await (const part of socket.download(message, state.controller.signal)) {
        state.controller.signal.throwIfAborted();
        if (part.byteLength > limit - size) throw new MediaFault('too_large');
        for (let offset = 0; offset < part.byteLength; offset += CHUNK_BYTES) {
          state.controller.signal.throwIfAborted();
          if (this.host.current() !== socket) throw new MediaFault('forbidden');
          const chunk = part.subarray(offset, offset + CHUNK_BYTES);
          size += chunk.byteLength;
          hash.update(chunk);
          const ack = new Promise<void>((resolve, reject) => { state.ack = { seq, resolve, reject }; });
          this.host.emit({ type: 'media_chunk', transfer_id: state.id, seq, data: Buffer.from(chunk).toString('base64') });
          await ack;
          seq++;
        }
      }
      state.controller.signal.throwIfAborted();
      if (size === 0) throw new MediaFault('invalid');
      this.host.emit({ type: 'media_end', transfer_id: state.id, seq, size, sha256: hash.digest('hex') });
    } catch (error) { if (this.download === state) this.error(state.id, error); }
    finally { if (this.download === state) this.remove(); }
  }
  private error(id: string, error: unknown): void {
    if (this.download?.id === id) this.remove();
    this.host.emit({ type: 'media_error', transfer_id: id, code: error instanceof MediaFault ? error.code : 'download_failed' });
  }
  private remove(): void {
    const state = this.download;
    this.download = undefined;
    if (!state) return;
    clearTimeout(state.timer);
    state.controller.abort();
    state.ack?.reject(new MediaFault('expired'));
  }
  close(): void {
    if (this.download) this.error(this.download.id, new MediaFault('forbidden'));
    this.offers.clear();
  }
}
