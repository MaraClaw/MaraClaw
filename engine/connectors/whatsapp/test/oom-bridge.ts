import { Worker } from 'node:worker_threads';
import { workerLimits } from '../src/limits.js';
import { supervise } from '../src/supervisor.js';

supervise(new Worker(new URL('./oom-worker.js', import.meta.url), { stdout: true, stderr: true,
  resourceLimits: workerLimits(process.argv.slice(2)) }));
