// Half to all of the capped exponential delay, so clients that failed together do not return in lockstep.
export function jitteredBackoff(attempt: number, base: number, cap: number, random: () => number): number {
  return Math.round(Math.min(cap, base * 2 ** attempt) * (0.5 + 0.5 * random()))
}
