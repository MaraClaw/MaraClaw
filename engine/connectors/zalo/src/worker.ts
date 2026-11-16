import { parentPort } from 'node:worker_threads';
import { z } from 'zod';
import { closeReason, parentFrame } from './protocol.js';
import { Runtime } from './runtime.js';
import { createClient } from './sdk.js';

const port = parentPort;
if (!port) process.exit(1);
const runtime = new Runtime({ client: createClient, write: line => port.postMessage(line), exit: () => {
  port.postMessage(null);
  port.close();
} });
const control = z.strictObject({ type: z.literal('stop'), reason: closeReason });
port.on('message', (raw: unknown) => {
  try {
    const stop = control.safeParse(raw);
    if (stop.success) runtime.stop(stop.data.reason);
    else { runtime.receive(parentFrame.parse(raw)); port.postMessage('handled'); }
  } catch (error) {
    runtime.stop(error instanceof Error ? 'protocol_error' : 'transport_failed');
  }
});
process.on('uncaughtException', () => runtime.stop('transport_failed'));
process.on('unhandledRejection', () => runtime.stop('transport_failed'));
