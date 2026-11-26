import { Worker } from 'node:worker_threads';
import { workerLimits } from './limits.js';
import { supervise } from './supervisor.js';

// Isolate dependency console output from the protocol, and terminate all socket resources together.
supervise(new Worker(new URL('./worker.js', import.meta.url), { stdout: true, stderr: true,
  resourceLimits: workerLimits(process.argv.slice(2)) }));
