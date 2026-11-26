import { Worker } from 'node:worker_threads';
import { workerLimits } from './limits.js';
import { supervise } from './supervisor.js';

supervise(new Worker(new URL('./worker.js', import.meta.url), { stdout: true, stderr: true,
  resourceLimits: workerLimits(process.argv.slice(2)) }));
