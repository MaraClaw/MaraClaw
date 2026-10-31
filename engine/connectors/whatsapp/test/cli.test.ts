import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { test } from 'node:test';
import { createInterface } from 'node:readline';
import { child, token } from './support.js';

test('private pipe EOF closes the spawned bridge while initial credential commit is withheld', async () => {
  const process = spawn(processExecPath(), ['dist/src/bridge.js'], { stdio: ['pipe', 'pipe', 'pipe'] });
  const lines = createInterface({ input: process.stdout });
  const iterator = lines[Symbol.asyncIterator]();
  process.stdin.write(JSON.stringify({ type: 'init', id: 'init', token, creds: null, lease_ms: 10000 }) + '\n');
  const first = await iterator.next();
  assert.equal(first.done, false);
  const frame = child.parse(JSON.parse(first.value));
  assert.equal(frame.type, 'creds_set');
  assert.equal(frame.token, token);
  process.stdin.end();
  const second = await iterator.next();
  assert.equal(second.done, false);
  assert.equal(child.parse(JSON.parse(second.value)).type, 'closed');
  const [code] = await once(process, 'exit');
  assert.equal(code, 0);
  lines.close();
});

function processExecPath(): string { return process.execPath; }
