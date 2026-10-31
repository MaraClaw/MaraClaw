import { proto } from 'baileys';
import type { BaileysEventMap } from 'baileys';
import { normalizeJid, selfJid } from './input.js';
import type { Identity } from './input.js';
import type { ChildPayload, SendOutcome, SendCommand } from './protocol.js';
import type { Ack, Transport } from './transport.js';

type Entry = { readonly command: SendCommand; readonly socket: Transport;
  timer: ReturnType<typeof setTimeout> | undefined; active: boolean; outcome: SendOutcome | undefined };
export type SendContext = {
  readonly emit: (payload: ChildPayload) => void;
  readonly barrier: () => Promise<void>;
  readonly current: () => { readonly socket: Transport; readonly identity: Identity } | null;
  readonly failed: () => void;
};
export class Sends {
  private readonly commands = new Map<string, Entry>();
  private readonly ids = new Map<string, Entry>();
  constructor(private readonly context: SendContext) {}

  submit(command: SendCommand): void {
    const existing = this.commands.get(command.command_id);
    if (existing) {
      const old = existing.command;
      if (old.message_id !== command.message_id || old.self_jid !== command.self_jid || old.text !== command.text) {
        this.context.failed(); return;
      }
      if (existing.outcome !== undefined) this.report(existing, existing.outcome);
      return;
    }
    if (this.commands.size >= 10_000 || this.ids.has(command.message_id)) { this.context.failed(); return; }
    const current = this.context.current();
    if (!current || !selfJid(normalizeJid(command.self_jid), current.identity) || command.self_jid !== normalizeJid(command.self_jid)) {
      // Stop on a rejected target so its replay can never later become authorized.
      this.context.emit({ type: 'send_result', command_id: command.command_id, message_id: command.message_id, outcome: 'rejected' });
      this.context.failed(); return;
    }
    const entry: Entry = { command, socket: current.socket, active: false, outcome: undefined, timer: undefined };
    this.commands.set(command.command_id, entry);
    this.ids.set(command.message_id, entry);
    entry.timer = setTimeout(() => this.finish(entry, 'uncertain'), command.timeout_ms);
    void this.dispatch(entry);
  }

  private async dispatch(entry: Entry): Promise<void> {
    try {
      await this.context.barrier();
      const current = this.context.current();
      if (entry.outcome !== undefined) return;
      if (!current || current.socket !== entry.socket || !selfJid(entry.command.self_jid, current.identity)) {
        this.finish(entry, 'rejected'); return;
      }
      entry.active = true;
      await entry.socket.send(entry.command.self_jid, entry.command.text, entry.command.message_id);
    } catch { // Once dispatched, transport exceptions cannot prove nonacceptance.
      this.finish(entry, entry.active ? 'uncertain' : 'rejected');
    }
  }

  ack(socket: Transport, ack: Ack): void {
    const entry = this.ids.get(ack.id);
    if (!entry || entry.socket !== socket || !entry.active || entry.outcome !== undefined) return;
    if (ack.jid !== null && normalizeJid(ack.jid) !== entry.command.self_jid) return;
    this.finish(entry, ack.accepted ? 'accepted' : 'rejected');
  }

  updates(socket: Transport, updates: BaileysEventMap['messages.update']): void {
    for (const { key, update } of updates) {
      if (!key.id || key.fromMe !== true || !key.remoteJid) continue;
      const status = update.status;
      if (status === proto.WebMessageInfo.Status.ERROR) {
        this.ack(socket, { id: key.id, jid: key.remoteJid, accepted: false });
      } else if (status != null && status >= proto.WebMessageInfo.Status.SERVER_ACK && status <= proto.WebMessageInfo.Status.PLAYED) {
        this.ack(socket, { id: key.id, jid: key.remoteJid, accepted: true });
      }
    }
  }

  disconnect(socket: Transport): void {
    for (const entry of this.commands.values()) if (entry.socket === socket) this.finish(entry, 'uncertain');
  }
  close(): void { for (const entry of this.commands.values()) this.finish(entry, 'uncertain'); }
  private finish(entry: Entry, outcome: SendOutcome): void {
    if (entry.outcome !== undefined) return;
    clearTimeout(entry.timer);
    entry.outcome = outcome;
    this.report(entry, outcome);
  }
  private report(entry: Entry, outcome: SendOutcome): void {
    this.context.emit({ type: 'send_result', command_id: entry.command.command_id, message_id: entry.command.message_id, outcome });
  }
}
