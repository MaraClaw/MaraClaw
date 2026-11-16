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
for (const ok of [true, false]) {
  test(`real pipes carry post-ready refresh ACK ${ok} and gate authorized typing and sends`, { timeout: 5000 }, async context => {
    const p = launch(context, 'heartbeat');
    p.write({ ...init, lease_ms: 120_000, keepalive_interval_ms: 60_000 });
    const initial = await p.next();
    assert.equal(initial.type, 'creds_set');
    p.write({ id: 'ack-initial', token, type: 'result', request_id: initial.id, ok: true });
    assert.equal((await p.next()).type, 'identity');
    assert.equal((await p.next()).type, 'ready');
    const refresh = await p.next();
    assert.equal(refresh.type, 'creds_set');
    assert.notEqual(refresh.id, initial.id);
    if (refresh.type === 'creds_set') assert.equal(JSON.parse(refresh.creds).cookie[0].value, 'refreshed-cookie');
    const typing = { id: 'typing', token, type: 'typing', thread_id: '20002' };
    const send = { id: 'send', token, type: 'send', command_id: 'blocked', thread_id: '20002', text: 'hello' };
    p.write(typing);
    p.write(send);
    const blocked = await p.next();
    assert.equal(blocked.type, 'send_result');
    if (blocked.type === 'send_result') assert.equal(blocked.outcome, 'rejected');
    p.write({ id: 'ack-refresh', token, type: 'result', request_id: refresh.id, ok });
    if (ok) {
      const heartbeat = await p.next();
      assert.equal(heartbeat.type, 'heartbeat');
      if (heartbeat.type === 'heartbeat') assert.equal(heartbeat.ok, true);
      p.write(typing);
      p.write({ ...send, command_id: 'after-refresh' });
      const result = await p.next();
      assert.equal(result.type, 'send_result');
      if (result.type === 'send_result') assert.equal(result.outcome, 'accepted');
      p.write({ id: 'bye', token, type: 'shutdown' });
    }
    const closed = await p.next();
    assert.equal(closed.type, 'closed');
    if (closed.type === 'closed') assert.equal(closed.reason, ok ? 'shutdown' : 'auth_store_failed');
    assert.deepEqual(await p.exit, [0, null]);
  });
}
