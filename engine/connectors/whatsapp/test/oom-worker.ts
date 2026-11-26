import { parentPort } from 'node:worker_threads';

const port = parentPort;
if (!port) process.exit(1);
// Bounded, so a heap ceiling that is not applied ends in a failed assertion instead of a runaway allocation.
port.on('message', () => {
  const hoard: number[][] = [];
  for (let index = 0; index < 512; index++) hoard.push(new Array<number>(100_000).fill(index));
});
