import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import { test } from 'node:test';
import type { TestContext } from 'node:test';
import { childFrame, MAX_FRAME_BYTES } from '../src/protocol.js';
import { credentials, token } from './support.js';

function launch(context: TestContext, mode = 'fixture') {
  const entry = mode === 'production' ? '../src/bridge.js' : './fixtures/bridge.js';
  const child = spawn(process.execPath, [new URL(entry, import.meta.url).pathname, mode], {
    stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env },
  });
  const exit = once(child, 'exit');
  const lines = createInterface({ input: child.stdout });
  const iterator = lines[Symbol.asyncIterator]();
  let stderr = '';
  child.stderr.on('data', (chunk: Buffer) => { stderr += chunk.toString(); });
  context.after(async () => {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
    await exit;
    lines.close();
    assert.equal(stderr, '');
  });
  return { child, exit, write: (frame: unknown) => child.stdin.write(JSON.stringify(frame) + '\n'),
    next: async () => {
      const line = await iterator.next();
      assert.equal(line.done, false);
      return childFrame.parse(JSON.parse(line.value));
    } };
}
const init = { id: 'init', token, type: 'init', lease_ms: 30_000, credentials: JSON.stringify(credentials) } as const;
test('actual production entry rejects invalid restore without SDK networking or error disclosure', { timeout: 5000 }, async context => {
  const p = launch(context, 'production');
  p.write({ ...init, credentials: '{}' });
  const frame = await p.next();
  assert.equal(frame.type, 'closed');
  if (frame.type === 'closed') assert.equal(frame.reason, 'login_failed');
  assert.equal(frame.token, token);
  assert.deepEqual(await p.exit, [0, null]);
});
test('stdout worker isolation, correlated commit and explicit shutdown work over real pipes', { timeout: 5000 }, async context => {
  const p = launch(context);
  p.write(init);
  const commit = await p.next();
  assert.equal(commit.type, 'creds_set');
  p.write({ id: 'ack', token, type: 'result', request_id: commit.id, ok: true });
  assert.equal((await p.next()).type, 'identity');
  assert.equal((await p.next()).type, 'ready');
  p.write({ id: 'bye', token, type: 'shutdown' });
  const closed = await p.next();
  assert.equal(closed.type, 'closed');
  if (closed.type === 'closed') assert.equal(closed.reason, 'shutdown');
  assert.deepEqual(await p.exit, [0, null]);
});
for (const mode of ['eof', 'lease', 'wrong-token', 'second-init', 'oversize', 'malformed-send', 'signal'] as const) {
  test(`pipe ${mode} closes and reaps worker while credential acknowledgment is withheld`, { timeout: 5000 }, async context => {
    const p = launch(context);
    p.write({ ...init, lease_ms: mode === 'lease' ? 800 : 30_000 });
    assert.equal((await p.next()).type, 'creds_set');
    switch (mode) {
      case 'eof': p.child.stdin.end(); break;
      case 'lease': break;
      case 'wrong-token': p.write({ type: 'lease', id: 'wrong', token: 'wrong-token', lease_ms: 30_000 }); break;
      case 'second-init': p.write(init); break;
      case 'oversize': p.child.stdin.write(Buffer.alloc(MAX_FRAME_BYTES + 1, 32)); break;
      case 'malformed-send': p.write({ type: 'send', id: 's', token, command_id: 'c', thread_id: '20002', text: '' }); break;
      case 'signal': p.child.kill('SIGTERM'); break;
    }
    const closed = await p.next();
    assert.equal(closed.type, 'closed');
    if (closed.type === 'closed') assert.equal(closed.reason,
      mode === 'eof' || mode === 'signal' ? 'shutdown' : mode === 'lease' ? 'lease_expired' : 'protocol_error');
    assert.deepEqual(await p.exit, [0, null]);
  });
}
test('independent main-thread deadline reaps an unresponsive SDK worker', { timeout: 6000 }, async context => {
  const p = launch(context, 'stuck');
  p.write(init);
  assert.equal((await p.next()).type, 'qr');
  p.child.stdin.end();
  const closed = await p.next();
  assert.equal(closed.type, 'closed');
  if (closed.type === 'closed') assert.equal(closed.reason, 'shutdown');
  assert.deepEqual(await p.exit, [0, null]);
});
