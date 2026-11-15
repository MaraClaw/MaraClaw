import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Frames, MAX_FRAME_BYTES, parentFrame, textSchema } from '../src/protocol.js';

const init = { type: 'init', id: 'i', token: 'capability', lease_ms: 30_000, credentials: null } as const;
test('byte framing reassembles split UTF-8 and several strict frames', () => {
  const frames = new Frames();
  const bytes = Buffer.from(JSON.stringify({ ...init, id: 'Vietnamese-ệ' }) + '\n' + JSON.stringify(init) + '\n');
  const split = bytes.indexOf(Buffer.from('ệ')) + 1;
  assert.deepEqual([...frames.push(bytes.subarray(0, split))], []);
  assert.deepEqual([...frames.push(bytes.subarray(split))], [{ ...init, id: 'Vietnamese-ệ' }, init]);
});
test('byte framing refuses oversized lines before JSON parsing with and without newline', () => {
  for (const suffix of ['', '\n']) {
    const frames = new Frames();
    assert.deepEqual([...frames.push(Buffer.alloc(MAX_FRAME_BYTES, 32))], []);
    assert.throws(() => [...frames.push(Buffer.from('x' + suffix))], { reason: 'protocol_error' });
  }
});
test('invalid UTF-8, JSON and strict schema inputs are refused', () => {
  for (const input of [Buffer.from([0xff, 10]), Buffer.from('{\n'), Buffer.from('{}\n')]) {
    assert.throws(() => [...new Frames().push(input)]);
  }
  for (const frame of [{ ...init, extra: true }, { ...init, lease_ms: 0 }, { ...init, lease_ms: 120_001 },
    { ...init, token: '' }, { type: 'result', id: 'r', token: 't', ok: true },
    { type: 'send', id: 's', token: 't', command_id: 'c', thread_id: 'group', text: 'hello' }]) {
    assert.equal(parentFrame.safeParse(frame).success, false);
  }
});
test('text limit counts Unicode code points and refuses empty content', () => {
  assert.equal(textSchema.safeParse('😀'.repeat(2000)).success, true);
  for (const text of ['', ' \n', '😀'.repeat(2001)]) assert.equal(textSchema.safeParse(text).success, false);
});
