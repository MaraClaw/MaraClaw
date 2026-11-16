import { readFileSync } from 'node:fs';
import { z } from 'zod';
import { parentFrame, messagePayload } from '../src/protocol.js';
import { mediaPayload } from '../src/media_protocol.js';

const envelope = z.looseObject({ id: z.string().min(1).max(256), token: z.string().min(1).max(512) });
const raw: unknown = JSON.parse(readFileSync(0, 'utf8'));
const frames = z.array(z.unknown()).parse(raw);
const results = frames.map(frame => {
  if (parentFrame.safeParse(frame).success) return true;
  const parsed = envelope.safeParse(frame);
  if (!parsed.success) return false;
  const { id: _id, token: _token, ...payload } = parsed.data;
  return messagePayload.safeParse(payload).success || mediaPayload.safeParse(payload).success;
});
process.stdout.write(JSON.stringify(results));
