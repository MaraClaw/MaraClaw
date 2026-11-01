import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Frames, MAX_FRAME_BYTES, parentFrame } from '../src/protocol.js';
import { Peer } from '../src/peer.js';
import { command, token } from './support.js';

test('parses a UTF-8 frame when bytes arrive in separate chunks', () => {
  const decoder = new Frames();
  const frame = command({ text: 'hello 한글' });
  const bytes = Buffer.from(JSON.stringify(frame) + '\n');
  const split = bytes.length - 5;
  assert.deepEqual(decoder.push(bytes.subarray(0, split)), []);
  assert.deepEqual(decoder.push(bytes.subarray(split)), [frame]);
});
test('rejects an oversized unterminated frame before parsing', () => {
  const decoder = new Frames();
  decoder.push(Buffer.alloc(MAX_FRAME_BYTES, 32));
  assert.throws(() => decoder.push(Buffer.from('x')));
});
test('rejects oversized terminated frames and malformed UTF-8', () => {
  assert.throws(() => new Frames().push(Buffer.alloc(MAX_FRAME_BYTES + 2, 10).fill(32, 0, MAX_FRAME_BYTES + 1)));
  assert.throws(() => new Frames().push(Buffer.from([0xff, 10])));
});
test('rejects unknown fields, malformed JSON, and unbounded send text', () => {
  assert.equal(parentFrame.safeParse({ ...command(), unexpected: 'secret' }).success, false);
  assert.equal(parentFrame.safeParse(command({ text: 'x'.repeat(4097) })).success, false);
  assert.throws(() => new Frames().push(Buffer.from('{bad}\n')));
});
test('accepts 4096 Unicode code points and rejects 4097', () => {
  assert.equal(parentFrame.safeParse(command({ text: '😀'.repeat(4096) })).success, true);
  assert.equal(parentFrame.safeParse(command({ text: '😀'.repeat(4097) })).success, false);
});
test('rejects a capability mismatch without emitting secret contents', () => {
  const output: string[] = [];
  const peer = new Peer(token, line => output.push(line));
  assert.throws(() => peer.authenticate('different-capability'));
  assert.deepEqual(output, []);
  peer.close();
});
test('rejects unrelated result IDs and bounds outgoing frames', () => {
  const peer = new Peer(token, () => undefined);
  assert.throws(() => peer.result({ type: 'result', id: 'unrelated', token, ok: true, value: null, error: null }));
  assert.throws(() => peer.emit({ type: 'creds_set', creds: 'x'.repeat(MAX_FRAME_BYTES) }));
  peer.close();
});
