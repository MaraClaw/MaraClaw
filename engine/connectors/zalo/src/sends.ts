import { BridgeFault } from './protocol.js';
import type { SendCommand, SendResult } from './protocol.js';
import { MediaFault, MEDIA_TIMEOUT_MS } from './media_protocol.js';

type Entry = { readonly command: SendCommand; result: SendResult | undefined; timer: NodeJS.Timeout | undefined };
export class Sends {
  private readonly commands = new Map<string, Entry>();
  private active = false;
  private pending = 0;
  constructor(private readonly emit: (result: SendResult) => void,
    private readonly dispatch: (command: SendCommand) => Promise<string | null>) {}
  open(): void { this.active = true; }
  pause(): void { this.active = false; }
  receive(command: SendCommand): void {
    const previous = this.commands.get(command.command_id);
    if (previous) {
      if (previous.command.thread_id !== command.thread_id || previous.command.text !== command.text ||
        JSON.stringify(previous.command.attachment) !== JSON.stringify(command.attachment)) {
        throw new BridgeFault('protocol_error');
      }
      if (previous.result) this.emit(previous.result);
      return;
    }
    if (this.commands.size >= 10_000) throw new BridgeFault('protocol_error');
    const entry: Entry = { command, result: undefined, timer: undefined };
    this.commands.set(command.command_id, entry);
    if (!this.active || this.pending >= 32) { this.finish(entry, { message_id: '', outcome: 'rejected' }); return; }
    this.pending++;
    entry.timer = setTimeout(() => this.finish(entry, { message_id: '', outcome: 'uncertain' }), command.attachment ? MEDIA_TIMEOUT_MS : 15_000);
    void this.dispatch(command).then(
      messageId => {
        this.pending--;
        this.finish(entry, { message_id: messageId ?? '', outcome: messageId ? 'accepted' : 'uncertain' });
      },
      error => {
        this.pending--;
        this.finish(entry, { message_id: '', outcome: error instanceof MediaFault ? 'rejected' : 'uncertain' });
      },
    );
  }
  private finish(entry: Entry, result: Pick<SendResult, 'message_id' | 'outcome'>): void {
    if (entry.result) return;
    clearTimeout(entry.timer);
    entry.timer = undefined;
    entry.result = { type: 'send_result', command_id: entry.command.command_id, ...result };
    this.emit(entry.result);
  }
  close(): void {
    this.active = false;
    for (const entry of this.commands.values()) this.finish(entry, { message_id: '', outcome: 'uncertain' });
  }
}
