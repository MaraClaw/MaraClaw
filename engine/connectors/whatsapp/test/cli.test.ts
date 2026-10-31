import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { test } from 'node:test';
import { createInterface } from 'node:readline';
import { diagnosticSchema } from '../src/diagnostics.js';
import { child, token } from './support.js';

test('private pipe EOF closes the spawned bridge while initial credential commit is withheld', async () => {
  const process = spawn(processExecPath(), ['dist/src/bridge.js'], { stdio: ['pipe', 'pipe', 'pipe'] });
  const lines = createInterface({ input: process.stdout });
  const iterator = lines[Symbol.asyncIterator]();
  process.stdin.write(JSON.stringify({ type: 'init', id: 'init', token, creds: null, lease_ms: 10000 }) + '\n');
  const nextBusiness = async () => {
    while (true) {
      const next = await iterator.next();
      assert.equal(next.done, false);
      const frame = child.parse(JSON.parse(next.value));
      if (frame.type !== 'diagnostic') return frame;
      assert.equal(frame.token, token);
      const { id: _id, token: _token, ...payload } = frame;
      diagnosticSchema.parse(payload);
    }
  };
  const frame = await nextBusiness();
  assert.equal(frame.type, 'creds_set');
  assert.equal(frame.token, token);
  process.stdin.end();
  assert.equal((await nextBusiness()).type, 'closed');
  const [code] = await once(process, 'exit');
  assert.equal(code, 0);
  lines.close();
});

function processExecPath(): string { return process.execPath; }
