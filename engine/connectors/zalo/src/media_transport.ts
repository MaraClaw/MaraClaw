import { createHash } from 'node:crypto';
import type { ChildPayload, ParentFrame, SendCommand } from './protocol.js';
import { assertNever } from './protocol.js';
import { CHUNK_BYTES, FILE_BYTES, IMAGE_BYTES, MEDIA_TIMEOUT_MS, MediaFault } from './media_protocol.js';
import { MediaOffers } from './media_offers.js';
import { downloadMedia } from './media_download.js';
import type { MediaCookies } from './media_download.js';

type MediaCommand = Extract<ParentFrame, { type: `media_${string}` }>;
type Stage = { readonly size: number; readonly sha256: string; readonly data: Buffer;
  readonly controller: AbortController; readonly timer: NodeJS.Timeout; received: number; seq: number; complete: boolean; held: boolean };
type Download = { readonly controller: AbortController; readonly timer: NodeJS.Timeout;
  ack: { readonly seq: number; readonly resolve: () => void; readonly reject: (error: MediaFault) => void } | undefined };
type Host = { readonly emit: (payload: ChildPayload) => void; readonly cookies: () => MediaCookies;
  readonly allowed: () => boolean; readonly hosts: readonly string[] };

export class MediaTransport {
  readonly offers = new MediaOffers();
  private readonly stages = new Map<string, Stage>();
  private readonly downloads = new Map<string, Download>();
  private bytes = 0;
  private active = true;
  constructor(private readonly host: Host) {}
  receive(frame: MediaCommand): void {
    try {
      switch (frame.type) {
        case 'media_error': this.remove(frame.transfer_id); return;
        case 'media_ack': {
          const state = this.downloads.get(frame.transfer_id);
          if (!state?.ack || state.ack.seq !== frame.seq) throw new MediaFault('invalid');
          const ack = state.ack;
          state.ack = undefined;
          ack.resolve();
          return;
        }
        case 'media_get':
          if (!this.host.allowed()) throw new MediaFault('forbidden');
          this.reserve(frame.transfer_id, FILE_BYTES);
          void this.get(frame);
          return;
        case 'media_put': {
          if (!this.host.allowed()) throw new MediaFault('forbidden');
          this.reserve(frame.transfer_id, frame.size);
          const controller = new AbortController();
          this.stages.set(frame.transfer_id, { size: frame.size, sha256: frame.sha256, data: Buffer.alloc(frame.size),
            received: 0, seq: 0, complete: false, held: false, controller,
            timer: setTimeout(() => this.error(frame.transfer_id, new MediaFault('expired')), MEDIA_TIMEOUT_MS) });
          this.bytes += frame.size;
          this.host.emit({ type: 'media_ready', transfer_id: frame.transfer_id, phase: 'receiving' });
          return;
        }
        case 'media_chunk': {
          const state = this.stages.get(frame.transfer_id);
          if (!state || state.complete || frame.seq !== state.seq) throw new MediaFault('invalid');
          const data = Buffer.from(frame.data, 'base64');
          if (state.received + data.length > state.size) throw new MediaFault('too_large');
          data.copy(state.data, state.received);
          state.received += data.length;
          state.seq++;
          this.host.emit({ type: 'media_ack', transfer_id: frame.transfer_id, seq: frame.seq });
          return;
        }
        case 'media_end': {
          const state = this.stages.get(frame.transfer_id);
          if (!state || state.complete) throw new MediaFault('invalid');
          if (state.received !== state.size || frame.size !== state.size || frame.sha256 !== state.sha256 ||
            createHash('sha256').update(state.data).digest('hex') !== state.sha256) throw new MediaFault('digest_mismatch');
          state.complete = true;
          this.host.emit({ type: 'media_ready', transfer_id: frame.transfer_id, phase: 'complete' });
          return;
        }
        default: assertNever(frame);
      }
    } catch (error) { this.error(frame.transfer_id, error); }
  }
  private reserve(id: string, size: number): void {
    if (this.stages.has(id) || this.downloads.has(id)) throw new MediaFault('invalid');
    if (this.stages.size + this.downloads.size >= 2 || this.bytes + size > 100 * 1024 * 1024) throw new MediaFault('too_large');
  }
  private async get(frame: Extract<MediaCommand, { type: 'media_get' }>): Promise<void> {
    const controller = new AbortController();
    const state: Download = { controller, ack: undefined,
      timer: setTimeout(() => this.error(frame.transfer_id, new MediaFault('expired')), MEDIA_TIMEOUT_MS) };
    this.downloads.set(frame.transfer_id, state);
    this.bytes += FILE_BYTES;
    try {
      const offer = this.offers.get(frame.message_id, frame.source_id);
      const data = await downloadMedia(offer.href, Math.min(frame.limit, offer.kind === 'image' ? IMAGE_BYTES : FILE_BYTES),
        { hosts: this.host.hosts, cookies: this.host.cookies(), signal: controller.signal });
      for (let offset = 0, seq = 0; offset < data.length; offset += CHUNK_BYTES, seq++) {
        controller.signal.throwIfAborted();
        const ack = new Promise<void>((resolve, reject) => { state.ack = { seq, resolve, reject }; });
        this.host.emit({ type: 'media_chunk', transfer_id: frame.transfer_id, seq,
          data: data.subarray(offset, offset + CHUNK_BYTES).toString('base64') });
        await ack;
      }
      if (!this.active || controller.signal.aborted) return;
      this.host.emit({ type: 'media_end', transfer_id: frame.transfer_id, size: data.length,
        sha256: createHash('sha256').update(data).digest('hex') });
    } catch (error) { if (this.downloads.has(frame.transfer_id)) this.error(frame.transfer_id, error); }
    finally { this.remove(frame.transfer_id); }
  }
  take(command: SendCommand): Buffer | undefined {
    const attachment = command.attachment;
    if (!attachment) return;
    const state = this.stages.get(attachment.transfer_id);
    if (!state?.complete || state.held || state.size !== attachment.size || state.sha256 !== attachment.sha256 ||
      state.size > (attachment.kind === 'image' ? IMAGE_BYTES : FILE_BYTES)) throw new MediaFault('invalid');
    const data = state.data;
    state.held = true;
    clearTimeout(state.timer);
    return data;
  }
  release(command: SendCommand): void {
    if (command.attachment) this.remove(command.attachment.transfer_id, true);
  }
  private error(id: string, error: unknown): void {
    this.remove(id);
    if (this.active) this.host.emit({ type: 'media_error', transfer_id: id,
      code: error instanceof MediaFault ? error.code : 'download_failed' });
  }
  private remove(id: string, release = false): void {
    const stage = this.stages.get(id);
    if (stage && (!stage.held || release)) {
      clearTimeout(stage.timer); stage.controller.abort(); this.bytes -= stage.size; this.stages.delete(id);
    }
    const download = this.downloads.get(id);
    if (download) {
      clearTimeout(download.timer); download.controller.abort();
      download.ack?.reject(new MediaFault('expired'));
      this.bytes -= FILE_BYTES;
      this.downloads.delete(id);
    }
  }
  close(): void {
    this.active = false;
    this.offers.close();
    for (const id of this.stages.keys()) this.remove(id, true);
    for (const id of this.downloads.keys()) this.remove(id);
  }
}
