import { randomUUID } from 'node:crypto';
import type { Worker } from 'node:worker_threads';
import { childFrame, Frames, MAX_FRAME_BYTES } from './protocol.js';
import type { CloseReason, ParentFrame } from './protocol.js';
import { sameToken } from './peer.js';

/** Owns stdio and hard deadlines independently of a stuck SDK worker. */
export function supervise(worker: Worker): void {
  worker.stdout?.resume();
  worker.stderr?.resume();
  const frames = new Frames();
  let token: string | undefined;
  let stopping = false;
  let exiting = false;
  let closed = false;
  let queued = 0;
  let lease: NodeJS.Timeout | undefined;
  let reap: NodeJS.Timeout | undefined;
  const init = setTimeout(() => stop('protocol_error'), 15_000);
  function write(line: string): void {
    if (process.stdout.writableLength > 2 * MAX_FRAME_BYTES) { exit(); return; }
    process.stdout.write(line);
  }
  function finish(reason: CloseReason): void {
    if (token !== undefined && !closed) {
      closed = true;
      write(JSON.stringify({ id: randomUUID(), token, type: 'closed', reason }) + '\n');
    }
    exit();
  }
  function exit(): void {
    if (exiting) return;
    exiting = true;
    clearTimeout(init); clearTimeout(lease); clearTimeout(reap);
    process.stdin.destroy();
    void worker.terminate().then(() => process.stdout.end(), () => process.stdout.end());
    setTimeout(() => process.exit(0), 1000).unref();
  }
  function stop(reason: CloseReason): void {
    if (stopping || exiting) return;
    stopping = true;
    clearTimeout(init); clearTimeout(lease);
    worker.postMessage({ type: 'stop', reason });
    reap = setTimeout(() => finish(reason), 2000);
  }
  function forward(frame: ParentFrame): void {
    if (stopping || exiting) return;
    if (token === undefined) {
      if (frame.type !== 'init') { stop('protocol_error'); return; }
      token = frame.token;
      clearTimeout(init);
    } else if (frame.type === 'init' || !sameToken(token, frame.token)) {
      stop('protocol_error'); return;
    }
    if (frame.type === 'shutdown') { stop('shutdown'); return; }
    if (frame.type === 'init' || frame.type === 'lease') {
      clearTimeout(lease);
      lease = setTimeout(() => stop('lease_expired'), frame.lease_ms);
    }
    if (++queued > 128) { stop('protocol_error'); return; }
    worker.postMessage(frame);
  }
  worker.on('message', (raw: unknown) => {
    if (exiting) return;
    if (raw === null) { finish('transport_failed'); return; }
    if (raw === 'handled') { queued--; return; }
    try {
      if (typeof raw !== 'string' || !raw.endsWith('\n') || Buffer.byteLength(raw) > MAX_FRAME_BYTES + 1) {
        stop('protocol_error'); return;
      }
      const value: unknown = JSON.parse(raw);
      const frame = childFrame.parse(value);
      if (token === undefined || !sameToken(token, frame.token) || closed) { stop('protocol_error'); return; }
      if (stopping && frame.type !== 'closed' && frame.type !== 'send_result') return;
      if (frame.type === 'closed') closed = true;
      write(raw);
    } catch (error) { // Worker output is an untrusted boundary; never expose exception text.
      if (error instanceof Error) stop('protocol_error');
      else stop('transport_failed');
    }
  });
  worker.on('error', () => finish('transport_failed'));
  worker.on('exit', () => finish('transport_failed'));
  process.stdin.on('data', (chunk: Buffer) => {
    try { for (const frame of frames.push(chunk)) forward(frame); }
    catch (error) {
      if (error instanceof Error) stop('protocol_error');
      else stop('transport_failed');
    }
  });
  process.stdin.on('end', () => stop('shutdown'));
  process.stdin.on('error', () => stop('shutdown'));
  process.stdout.on('error', exit);
  process.once('SIGINT', () => stop('shutdown'));
  process.once('SIGTERM', () => stop('shutdown'));
  process.on('uncaughtException', () => finish('transport_failed'));
  process.on('unhandledRejection', () => finish('transport_failed'));
}
