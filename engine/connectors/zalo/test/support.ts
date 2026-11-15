import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { LoginQRCallbackEventType, ThreadType } from 'zca-js';
import type { Credentials, LoginQRCallback, Message, SendMessageResponse, TMessage } from 'zca-js';
import { childFrame } from '../src/protocol.js';
import type { ChildFrame } from '../src/protocol.js';
import { Runtime } from '../src/runtime.js';
import type { SdkApi, SdkClient, SdkListener } from '../src/sdk.js';

export const token = 'one-run-only-secret';
export const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2ioAAAAASUVORK5CYII=';
export const credentials = { cookie: [{ key: 'session', value: 'private-cookie', domain: 'zalo.me', path: '/' }],
  imei: 'resolved-imei', userAgent: 'resolved-user-agent' };
function isFrame<T extends ChildFrame['type']>(frame: ChildFrame, type: T): frame is Extract<ChildFrame, { type: T }> {
  return frame.type === type;
}
export class TestListener extends EventEmitter<{ message: [Message]; connected: []; closed: []; disconnected: []; error: [unknown] }> implements SdkListener {
  started = false;
  autoConnect = true;
  start(options: { retryOnClose: boolean }): void {
    assert.equal(options.retryOnClose, false);
    assert.equal(this.started, false);
    this.started = true;
    if (this.autoConnect) this.emit('connected');
  }
  stop(): void { this.started = false; }
}
export class TestApi implements SdkApi {
  readonly listener = new TestListener();
  readonly dispatched: { text: string; threadId: string; type: ThreadType.User }[] = [];
  response: () => Promise<SendMessageResponse> = async () => ({ message: { msgId: 97531 }, attachment: [] });
  getOwnId(): string { return '10001'; }
  getContext() { return { imei: credentials.imei, userAgent: credentials.userAgent,
    cookie: { toJSON: () => ({ cookies: credentials.cookie }) } }; }
  sendMessage(text: string, threadId: string, type: ThreadType.User): Promise<SendMessageResponse> {
    this.dispatched.push({ text, threadId, type });
    return this.response();
  }
}
export class TestSdk implements SdkClient {
  readonly api = new TestApi();
  readonly qr = Promise.withResolvers<SdkApi>();
  restored: Credentials | undefined;
  callback: LoginQRCallback | undefined;
  qrCalls = 0;
  aborted = 0;
  settleAbort = true;
  restoreFailure = false;
  async login(value: Credentials): Promise<SdkApi> {
    this.restored = value;
    if (this.restoreFailure) throw new Error('private-provider-failure');
    return this.api;
  }
  loginQR(_options: Record<string, never>, callback: LoginQRCallback): Promise<SdkApi> {
    this.qrCalls++;
    this.callback = callback;
    return this.qr.promise;
  }
  generateQr(): void {
    this.callback?.({ type: LoginQRCallbackEventType.QRCodeGenerated,
      data: { code: 'raw-qr-must-not-leak', image: png, options: { enabledCheckOCR: false, enabledMultiLayer: false }, token: 'sdk-private-token' },
      actions: { abort: () => { this.aborted++; if (this.settleAbort) this.qr.reject(new Error('aborted')); }, retry: () => assert.fail('no retry'),
        saveToFile: async () => assert.fail('no disk') } });
  }
  completeQr(): void { this.qr.resolve(this.api); }
}
export class Harness {
  readonly frames: ChildFrame[] = [];
  readonly events = new EventEmitter<{ frame: [ChildFrame] }>();
  readonly sdk = new TestSdk();
  exits = 0;
  readonly runtime = new Runtime({ client: () => this.sdk, write: line => {
    const frame = childFrame.parse(JSON.parse(line));
    this.frames.push(frame);
    this.events.emit('frame', frame);
  }, exit: () => { this.exits++; } });
  init(restored: string | null = JSON.stringify(credentials)): void {
    this.runtime.receive({ type: 'init', id: 'init', token, lease_ms: 120_000, credentials: restored });
  }
  waitFor<T extends ChildFrame['type']>(type: T, after = 0): Promise<Extract<ChildFrame, { type: T }>> {
    return new Promise((resolve, reject) => {
      const existing = this.frames.slice(after).find(frame => isFrame(frame, type));
      if (existing) { resolve(existing); return; }
      const timer = setTimeout(() => { this.events.off('frame', receive); reject(new Error(`missing ${type}`)); }, 2000);
      const receive = (frame: ChildFrame): void => {
        if (!isFrame(frame, type)) return;
        clearTimeout(timer);
        this.events.off('frame', receive);
        resolve(frame);
      };
      this.events.on('frame', receive);
    });
  }
  ack(requestId: string, ok = true): void {
    this.runtime.receive({ type: 'result', id: 'ack', token, request_id: requestId, ok });
  }
  async connect(): Promise<void> {
    this.init();
    this.ack((await this.waitFor('creds_set')).id);
    await this.waitFor('ready');
  }
}
export function messageData(overrides: Partial<TMessage> = {}): TMessage {
  return { actionId: '1', msgId: 'message-1', cliMsgId: '2', msgType: 'webchat', uidFrom: '20002', idTo: '10001',
    dName: 'display-name-is-not-identity', ts: '1700000000123', status: 1, content: 'hello from Zalo', notify: '', ttl: 0,
    userId: '', uin: '', topOut: '', topOutTimeOut: '', topOutImprTimeOut: '', propertyExt: undefined,
    paramsExt: { countUnread: 1, containType: 0, platformType: 1 }, cmd: 0, st: 0, at: 0, realMsgId: '', quote: undefined, ...overrides };
}
