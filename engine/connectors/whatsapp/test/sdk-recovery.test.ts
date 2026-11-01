import assert from 'node:assert/strict';
import { test } from 'node:test';
import { proto } from 'baileys';
import type { WAMessage } from 'baileys';
import { notifyText, socketIdentity } from '../src/input.js';
import { selfMessage, sdkHarness } from './sdk-recovery.js';
import type { Upsert } from './sdk-recovery.js';

const identity = socketIdentity({ id: '15551234567:2@s.whatsapp.net', lid: '123456789:2@lid' });
const forwarded = (events: readonly Upsert[]): string[] =>
  events.flatMap(event => notifyText(event, identity)).map(payload => payload.type === 'message' ? payload.message_id : '');
const text = (value: string): proto.IMessage => ({ conversation: value });

test('SDK-recovered second and third texts are forwarded by native ID after an ordinary first and assistant ACK', async () => {
  // Given an ordinary live first text and the assistant's own acknowledged reply.
  const sdk = sdkHarness();
  sdk.live(selfMessage('FIRST', text('first')));
  sdk.live(selfMessage('ASSISTANT-ACK', text('reply')));
  // When the phone answers the SDK's placeholder resend requests for the next two texts.
  await sdk.recover(selfMessage('SECOND', text('second')), 'REQUEST-2');
  await sdk.recover(selfMessage('THIRD', text('third')), 'REQUEST-3');
  // Then the SDK's own events carry request IDs only for the recovered texts, and every native ID is forwarded.
  assert.deepEqual(sdk.seen.map(event => event.requestId), [undefined, undefined, 'REQUEST-2', 'REQUEST-3']);
  assert.deepEqual(sdk.seen.map(event => event.type), ['notify', 'notify', 'notify', 'notify']);
  assert.deepEqual(forwarded(sdk.seen), ['FIRST', 'ASSISTANT-ACK', 'SECOND', 'THIRD']);
});

test('buffered SDK consolidation drops the correlation ID, so it cannot be a history marker', async () => {
  // Given recovered and ordinary messages consolidated by the SDK event buffer.
  const sdk = sdkHarness();
  sdk.ev.buffer();
  await sdk.recover(selfMessage('SECOND', text('second')), 'REQUEST-2');
  sdk.live(selfMessage('FIRST', text('first')));
  // When the buffer flushes.
  sdk.ev.flush();
  // Then the consolidated upsert has no request ID and both texts are forwarded.
  assert.equal(sdk.seen.length, 1);
  assert.equal(sdk.seen[0]?.requestId, undefined);
  assert.deepEqual(forwarded(sdk.seen).toSorted(), ['FIRST', 'SECOND']);
});

test('SDK protobuf decoding leaves an ordinary recovered text with a null stub type', async () => {
  // Given a recovered text decoded by the SDK from its wire bytes.
  const sdk = sdkHarness();
  await sdk.recover(selfMessage('SECOND', text('second')), 'REQUEST-2');
  // Then the stub type is the decoded null default, which the unchanged stub predicate accepts.
  assert.equal(sdk.seen[0]?.messages[0]?.messageStubType, null);
  assert.deepEqual(forwarded(sdk.seen), ['SECOND']);
});

const stub = { messageStubType: proto.WebMessageInfo.StubType.CIPHERTEXT };
const rejected: readonly (readonly [string, WAMessage])[] = [
  ['another chat', selfMessage('X', text('x'), { key: { remoteJid: '15559990000@s.whatsapp.net', fromMe: true, id: 'X' } })],
  ['media', selfMessage('X', { imageMessage: { caption: 'x' } })],
  ['quoted reply', selfMessage('X', { extendedTextMessage: { text: 'x', contextInfo: { stanzaId: 'Q', quotedMessage: text('q') } } })],
  ['unsupported wrapper', selfMessage('X', { ephemeralMessage: { message: text('x') } })],
  ['stub', selfMessage('X', text('x'), stub)],
];

for (const [name, message] of rejected) {
  test(`an SDK-recovered ${name} is still rejected`, async () => {
    const sdk = sdkHarness();
    await sdk.recover(message, 'REQUEST-X');
    assert.equal(sdk.seen[0]?.requestId, 'REQUEST-X');
    assert.deepEqual(forwarded(sdk.seen), []);
  });
}

test('history appends are rejected whether or not they carry a request ID', () => {
  const sdk = sdkHarness();
  sdk.live(selfMessage('OLD', text('old')), 'append');
  sdk.ev.emit('messages.upsert', { messages: [selfMessage('OLDER', text('older'))], type: 'append', requestId: 'history' });
  assert.deepEqual(forwarded(sdk.seen), []);
});
