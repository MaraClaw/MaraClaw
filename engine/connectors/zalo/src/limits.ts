import type { ResourceLimits } from 'node:worker_threads';

const flag = '--worker-heap-mb=';

/** The parent passes the SDK worker's V8 old-generation ceiling in MiB as `--worker-heap-mb=<n>`; without it Node's defaults apply. */
export function workerLimits(argv: readonly string[]): ResourceLimits {
  const given = argv.find(arg => arg.startsWith(flag));
  if (given === undefined) return {};
  const megabytes = given.slice(flag.length);
  if (!/^[1-9][0-9]{0,4}$/.test(megabytes)) throw new RangeError('invalid worker heap ceiling');
  return { maxOldGenerationSizeMb: Number(megabytes) };
}
