import { Worker } from 'node:worker_threads';
import { supervise } from './supervisor.js';

supervise(new Worker(new URL('./worker.js', import.meta.url), { stdout: true, stderr: true }));
