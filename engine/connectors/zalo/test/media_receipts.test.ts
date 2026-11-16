import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import type { SendMessageResponse } from 'zca-js';
import { parentFrame } from '../src/protocol.js';
import { bufferAttachment } from '../src/media_attachment.js';
import { Harness, png, token } from './support.js';

for (const [name, receipt] of [
  ['missing attachment', { message: null, attachment: [] }],
  ['multiple attachments', { message: null, attachment: [{ msgId: 1 }, { msgId: 2 }] }],
  ['invalid receipt id', { message: null, attachment: [{ msgId: -1 }] }],
  ['unexpected text receipt', { message: { msgId: 1 }, attachment: [{ msgId: 2 }] }],
] satisfies [string, SendMessageResponse][]) {
  test(`${name} remains uncertain and a replay never dispatches again`, async context => {
    const h = new Harness();
    context.after(() => h.runtime.stop('shutdown'));
    h.sdk.api.response = async () => receipt;
    await h.connect();
    const data = Buffer.from('abc');
    const sha256 = createHash('sha256').update(data).digest('hex');
    const common = { id: 'media', token, transfer_id: 't' };
    h.runtime.receive({ ...common, type: 'media_put', size: data.length, sha256 });
    h.runtime.receive({ ...common, type: 'media_chunk', seq: 0, data: data.toString('base64') });
    h.runtime.receive({ ...common, type: 'media_end', size: data.length, sha256 });
    const command = parentFrame.parse({ id: 'send', token, type: 'send', command_id: 'once', thread_id: '20002', text: '',
      attachment: { transfer_id: 't', filename: 'a.bin', kind: 'file', size: data.length, sha256 } });
    h.runtime.receive(command);
    assert.equal((await h.waitFor('send_result')).outcome, 'uncertain');
    h.runtime.receive(command);
    assert.equal(h.sdk.api.dispatched.length, 1);
  });
}

test('static image Buffer metadata uses Sharp dimensions; GIF and malformed images reject before dispatch', async () => {
  const data = Buffer.from(png, 'base64');
  const command = parentFrame.parse({ id: 'send', token, type: 'send', command_id: 'image', thread_id: '20002', text: '',
    attachment: { transfer_id: 't', filename: 'a.png', kind: 'image', size: data.length,
      sha256: createHash('sha256').update(data).digest('hex') } });
  assert.ok(command.type === 'send');
  const attachment = await bufferAttachment(command, data);
  assert.ok(typeof attachment !== 'string');
  assert.deepEqual(attachment.metadata, { totalSize: data.length, width: 1, height: 1 });
  assert.deepEqual(attachment.data, data);
  await assert.rejects(bufferAttachment(command, Buffer.from('broken')), { code: 'unsupported' });
  await assert.rejects(bufferAttachment(command, Buffer.from('GIF89a')), { code: 'unsupported' });
});
