import { randomUUID, timingSafeEqual } from 'node:crypto';
import { Worker } from 'node:worker_threads';
import { Frames, MAX_FRAME_BYTES } from './protocol.js';
import type { ParentFrame } from './protocol.js';

// Isolate dependency console output from the protocol, and terminate all socket resources together.
const worker = new Worker(new URL('./worker.js', import.meta.url), { stdout: true, stderr: true });
worker.stdout.resume();
worker.stderr.resume();
const frames = new Frames();
let exiting = false;
let token: string | undefined;
let lease: NodeJS.Timeout | undefined;
let stopTimer: NodeJS.Timeout | undefined;
function observe(stage: 'bridge_eof' | 'worker_exit', code?: number, failure = false): void {
  if (token === undefined || exiting || !process.stdout.writable) return;
  try { process.stdout.write(JSON.stringify({ type: 'diagnostic', id: randomUUID(), token, stage, event: failure || code !== undefined && code !== 0 ? 'failure' : 'finished',
    ...(code !== undefined && Number.isInteger(code) && code >= -255 && code <= 255 ? { exit_code: code } : {}) }) + '\n'); }
  catch { return; } // Fatal observation is best effort; shutdown remains unchanged.
}
const initTimer = setTimeout(() => shutdown(), 15_000);

const exit = (): void => {
  if (exiting) return;
  exiting = true;
  clearTimeout(initTimer); clearTimeout(lease); clearTimeout(stopTimer);
  process.stdin.destroy();
  void worker.terminate().then(() => process.stdout.end(), () => process.stdout.end());
  setTimeout(() => process.exit(0), 1000).unref();
};
function shutdown(): void {
  if (exiting || stopTimer) return;
  worker.postMessage(null);
  stopTimer = setTimeout(exit, 2250);
}
function forward(frame: ParentFrame): void {
  if (stopTimer || exiting) return;
  if (token === undefined) {
    if (frame.type !== 'init') { shutdown(); return; }
    token = frame.token;
    clearTimeout(initTimer);
  } else {
    const expected = Buffer.from(token);
    const received = Buffer.from(frame.token);
    if (frame.type === 'init' || expected.length !== received.length || !timingSafeEqual(expected, received)) {
      shutdown(); return;
    }
  }
  if (frame.type === 'init' || frame.type === 'lease') {
    clearTimeout(lease);
    lease = setTimeout(exit, frame.lease_ms + 2250);
  }
  worker.postMessage(frame);
}
worker.on('message', (raw: unknown) => {
  if (raw === null) { exit(); return; }
  if (typeof raw !== 'string' || Buffer.byteLength(raw) > MAX_FRAME_BYTES + 1 || !raw.endsWith('\n')) { exit(); return; }
  if (process.stdout.writableLength > 2 * MAX_FRAME_BYTES) { exit(); return; }
  process.stdout.write(raw);
});
worker.on('error', () => { observe('worker_exit', undefined, true); exit(); });
worker.on('exit', code => { observe('worker_exit', code); exit(); });
process.stdin.on('data', (chunk: Buffer) => {
  try { for (const frame of frames.push(chunk)) forward(frame); }
  catch { shutdown(); }
});
process.stdin.on('end', () => { observe('bridge_eof'); shutdown(); });
process.stdin.on('error', shutdown);
process.stdout.on('error', exit);
process.once('SIGTERM', shutdown);
process.once('SIGINT', shutdown);
process.on('uncaughtException', exit);
process.on('unhandledRejection', exit);
