import { Worker } from 'node:worker_threads';
import { supervise } from '../../src/supervisor.js';

supervise(new Worker(new URL('./worker.js', import.meta.url), {
  stdout: true, stderr: true, workerData: process.argv[2],
}));
