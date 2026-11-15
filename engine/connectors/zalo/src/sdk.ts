import { readFile } from 'node:fs/promises';
import sharp from 'sharp';
import { LoginQRCallbackEventType, ThreadType, Zalo } from 'zca-js';
import type { Credentials, LoginQRCallback, Message, SendMessageResponse } from 'zca-js';
import { z } from 'zod';
import { assertNever, BridgeFault, childFrame } from './protocol.js';
import type { ChildPayload, CloseReason, SendCommand } from './protocol.js';

export interface SdkListener {
  on(event: 'message', handler: (message: Message) => void): unknown;
  on(event: 'connected' | 'closed' | 'disconnected' | 'error', handler: () => void): unknown;
  start(options: { retryOnClose: boolean }): void;
  stop(): void;
}
export interface SdkApi {
  readonly listener: SdkListener;
  getOwnId(): string;
  getContext(): { readonly imei: string; readonly userAgent: string;
    readonly cookie: { toJSON(): { readonly cookies: unknown } | undefined } };
  sendMessage(text: string, threadId: string, type: ThreadType.User): Promise<SendMessageResponse>;
}
export interface SdkClient {
  login(credentials: Credentials): Promise<SdkApi>;
  loginQR(options: Record<string, never>, callback: LoginQRCallback): Promise<SdkApi>;
}
export const createClient = (): SdkClient => new Zalo({
  selfListen: false, checkUpdate: false, logging: false,
  imageMetadataGetter: async filePath => {
    const data = await readFile(filePath);
    const { width, height, size } = await sharp(data).metadata();
    if (!width || !height) return null;
    return { width, height, size: size ?? data.length };
  },
});
const credentialsSchema = z.strictObject({
  cookie: z.array(z.object({ key: z.string().min(1), value: z.string(), domain: z.string().min(1) }).catchall(z.json())).min(1).max(512),
  imei: z.string().min(1).max(512), userAgent: z.string().min(1).max(4096),
});
const sendReceipt = z.object({ message: z.object({ msgId: z.union([
  z.string().regex(/^[0-9]{1,256}$/), z.number().int().min(0).max(Number.MAX_SAFE_INTEGER),
]) }).nullable() });
type LoginEvents = { readonly qr: (value: string, expiresAt: string) => void; readonly closed: (reason: CloseReason) => void };
type ListenEvents = { readonly connected: () => void; readonly message: (payload: Extract<ChildPayload, { type: 'message' }>) => void;
  readonly closed: () => void };

export class SdkAdapter {
  private api: SdkApi | undefined;
  private active = true;
  private abort: (() => unknown) | undefined;
  private qrTimer: NodeJS.Timeout | undefined;
  constructor(private readonly client: SdkClient, private readonly events: LoginEvents) {}
  async login(credentials: string | null): Promise<void> {
    const api = credentials === null
      ? await this.client.loginQR({}, event => {
        if (event.actions) this.abort = event.actions.abort;
        if (!this.active) { this.abort?.(); return; }
        switch (event.type) {
          case LoginQRCallbackEventType.QRCodeGenerated:
            clearTimeout(this.qrTimer);
            this.qrTimer = setTimeout(() => this.events.closed('qr_expired'), 100_000);
            this.events.qr(event.data.image, new Date(Date.now() + 100_000).toISOString());
            return;
          case LoginQRCallbackEventType.QRCodeExpired: this.events.closed('qr_expired'); return;
          case LoginQRCallbackEventType.QRCodeDeclined: this.events.closed('qr_declined'); return;
          case LoginQRCallbackEventType.QRCodeScanned: return;
          case LoginQRCallbackEventType.GotLoginInfo: return;
          default: assertNever(event);
        }
      })
      : await this.client.login(credentialsSchema.parse(JSON.parse(credentials)));
    clearTimeout(this.qrTimer);
    this.abort = undefined;
    if (!this.active) { api.listener.stop(); throw new BridgeFault('shutdown'); }
    this.api = api;
  }
  credentials(): string {
    const context = this.session().getContext();
    const credentials = credentialsSchema.parse({ cookie: context.cookie.toJSON()?.cookies, imei: context.imei, userAgent: context.userAgent });
    return JSON.stringify(credentials);
  }
  accountId(): string { return this.session().getOwnId(); }
  listen(events: ListenEvents): void {
    const api = this.session();
    api.listener.on('connected', events.connected);
    api.listener.on('disconnected', events.closed);
    api.listener.on('closed', events.closed);
    api.listener.on('error', events.closed);
    api.listener.on('message', message => {
      if (!this.active) return;
      switch (message.type) {
        case ThreadType.Group: return;
        case ThreadType.User: {
          if (message.isSelf || message.data.uidFrom === api.getOwnId() || message.data.msgType !== 'webchat' ||
            typeof message.data.content !== 'string' || !/^\d+$/.test(message.data.ts)) return;
          const milliseconds = Number(message.data.ts);
          if (!Number.isSafeInteger(milliseconds)) return;
          const frame = childFrame.safeParse({ id: 'message', token: 'validation', type: 'message',
            message_id: message.data.msgId, sender_id: message.data.uidFrom, thread_id: message.threadId,
            from_me: false, timestamp: Math.floor(milliseconds / 1000), text: message.data.content });
          if (frame.success && frame.data.type === 'message') events.message(frame.data);
          return;
        }
        default: assertNever(message);
      }
    });
    api.listener.start({ retryOnClose: false });
  }
  async send(command: SendCommand): Promise<string | null> {
    const response = await this.session().sendMessage(command.text, command.thread_id, ThreadType.User);
    const receipt = sendReceipt.safeParse(JSON.parse(JSON.stringify(response)));
    return receipt.success && receipt.data.message !== null ? String(receipt.data.message.msgId) : null;
  }
  close(): void {
    this.active = false;
    clearTimeout(this.qrTimer);
    try { this.abort?.(); }
    finally { this.api?.listener.stop(); }
    this.abort = undefined;
  }
  private session(): SdkApi {
    if (!this.api || !this.active) throw new BridgeFault('transport_failed');
    return this.api;
  }
}
