import { parentPort, workerData } from 'node:worker_threads';
import { mock } from 'node:test';
import { z } from 'zod';
import { closeReason, parentFrame } from '../../src/protocol.js';
import { Runtime } from '../../src/runtime.js';
import { png, TestSdk } from '../support.js';

const port = parentPort;
if (!port) process.exit(1);
console.log('SDK stdout secret must be isolated');
console.error('SDK stderr secret must be discarded');
const sdk = new TestSdk();
if (workerData === 'heartbeat') {
  mock.timers.enable({ apis: ['setTimeout'] });
  sdk.api.ping = async () => {
    sdk.api.cookies[0] = { key: 'session', value: 'refreshed-cookie', domain: 'zalo.me', path: '/' };
  };
  sdk.api.response = async () => ({ message: sdk.api.typed.length === 1 && sdk.api.pingCalls === 1 ? { msgId: 97531 } : null, attachment: [] });
}
let acknowledgments = 0;
const runtime = new Runtime({ client: () => sdk, write: line => port.postMessage(line), exit: () => {
  port.postMessage(null); port.close();
} });
const control = z.strictObject({ type: z.literal('stop'), reason: closeReason });
port.on('message', (raw: unknown) => {
  const stop = control.safeParse(raw);
  if (stop.success) { runtime.stop(stop.data.reason); return; }
  const frame = parentFrame.parse(raw);
  if (workerData === 'stuck') {
    port.postMessage(JSON.stringify({ id: 'qr', token: frame.token, type: 'qr', value: png,
      expires_at: new Date(Date.now() + 100_000).toISOString() }) + '\n');
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0);
    return;
  }
  runtime.receive(frame);
  if (workerData === 'heartbeat' && frame.type === 'result' && acknowledgments++ === 0) {
    setImmediate(() => mock.timers.tick(60_000));
  }
  port.postMessage('handled');
});
