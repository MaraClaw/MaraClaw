import { BufferJSON, initAuthCreds, proto } from 'baileys';
import type { AuthenticationCreds, SignalDataTypeMap } from 'baileys';
import { z } from 'zod';

const bytes = z.instanceof(Uint8Array);
const protobufBytes = z.union([bytes, z.base64().transform(value => Buffer.from(value, 'base64'))]);
const buffer = z.instanceof(Buffer);
const pair = z.object({ public: bytes, private: bytes });
const account = z.object({ details: bytes.nullish(), accountSignatureKey: bytes.nullish(),
  accountSignature: bytes.nullish(), deviceSignature: bytes.nullish() }).passthrough();
const storedAccount = account.extend({ details: protobufBytes.nullish(), accountSignatureKey: protobufBytes.nullish(),
  accountSignature: protobufBytes.nullish(), deviceSignature: protobufBytes.nullish() });
const contact = z.object({ id: z.string(), lid: z.string().optional(), phoneNumber: z.string().optional(),
  name: z.string().optional(), notify: z.string().optional(), verifiedName: z.string().optional() }).passthrough();
const credentials = z.object({
  signedIdentityKey: pair, noiseKey: pair, pairingEphemeralKeyPair: pair,
  signedPreKey: z.object({ keyPair: pair, signature: bytes, keyId: z.number().int(), timestampS: z.number().optional() }),
  registrationId: z.number().int(), advSecretKey: z.string(), me: contact.optional(),
  firstUnuploadedPreKeyId: z.number().int(), nextPreKeyId: z.number().int(),
  processedHistoryMessages: z.array(z.object({ key: z.object({}).passthrough() }).passthrough()),
  accountSyncCounter: z.number().int(), registered: z.boolean(),
  accountSettings: z.object({ unarchiveChats: z.boolean() }).passthrough(),
  signalIdentities: z.array(z.object({ identifier: z.object({ name: z.string(), deviceId: z.number().int() }), identifierKey: bytes })).optional(),
  account: account.optional(),
  myAppStateKeyId: z.string().optional(), lastAccountSyncTimestamp: z.number().optional(),
  platform: z.string().optional(), pairingCode: z.string().optional(), lastPropHash: z.string().optional(),
  routingInfo: buffer.optional(),
}).passthrough();

// Preserve the original BufferJSON value, including provider fields unknown to this release.
const credentialValue = z.custom<AuthenticationCreds>(value => credentials.safeParse(value).success);
export function decodeCreds(encoded: string | null): AuthenticationCreds {
  if (encoded === null) return initAuthCreds();
  const value = z.record(z.string(), z.unknown()).parse(JSON.parse(encoded, BufferJSON.reviver));
  // Protobuf toJSON runs before BufferJSON's replacer and emits base64 byte fields.
  if (value['account'] !== undefined) value['account'] = storedAccount.parse(value['account']);
  return credentialValue.parse(value);
}
export function encode(value: unknown): string { return JSON.stringify(value, BufferJSON.replacer); }
export function decode(encoded: string): unknown { return JSON.parse(encoded, BufferJSON.reviver); }

const sessionBytes = z.base64().min(1);
const sessionChain = z.object({
  chainKey: z.object({ counter: z.number().int(), key: sessionBytes.nullish() }).passthrough(),
  chainType: z.number().int(), messageKeys: z.record(z.string(), sessionBytes),
}).passthrough();
const sessionEntry = z.object({
  registrationId: z.number().int(),
  currentRatchet: z.object({
    ephemeralKeyPair: z.object({ pubKey: sessionBytes, privKey: sessionBytes }).passthrough(),
    lastRemoteEphemeralKey: sessionBytes, previousCounter: z.number().int(), rootKey: sessionBytes,
  }).passthrough(),
  indexInfo: z.object({
    baseKey: sessionBytes, baseKeyType: z.number().int(), closed: z.number(), used: z.number(),
    created: z.number(), remoteIdentityKey: sessionBytes,
  }).passthrough(),
  _chains: z.record(z.string(), sessionChain),
  pendingPreKey: z.object({ baseKey: sessionBytes }).passthrough().optional(),
}).passthrough();
const sessionRecord = z.object({ _sessions: z.record(z.string(), sessionEntry),
  version: z.literal('v1') }).passthrough();
// libsignal's runtime serializes objects despite declaring this SDK storage slot as Uint8Array.
const sessionValue = z.custom<SignalDataTypeMap['session']>(value => sessionRecord.safeParse(value).success);
const parsers: { readonly [K in keyof SignalDataTypeMap]: (value: unknown) => SignalDataTypeMap[K] } = {
  'pre-key': value => pair.parse(value),
  session: value => sessionValue.parse(value),
  'sender-key': value => bytes.parse(value),
  'sender-key-memory': value => z.record(z.string(), z.boolean()).parse(value),
  'app-state-sync-key': value => {
    const object = z.object({ keyData: protobufBytes.nullable().optional(),
      fingerprint: z.object({ rawId: z.number().nullable().optional(), currentIndex: z.number().nullable().optional(),
        deviceIndexes: z.array(z.number()).nullable().optional() }).nullable().optional(),
      timestamp: z.union([z.number(), z.string(), z.object({ low: z.number(), high: z.number(), unsigned: z.boolean().optional() })]).nullable().optional(),
    }).parse(value);
    return proto.Message.AppStateSyncKeyData.fromObject(object);
  },
  'app-state-sync-version': value => z.object({ version: z.number(), hash: buffer,
    indexValueMap: z.record(z.string(), z.object({ valueMac: bytes })) }).parse(value),
  'lid-mapping': value => z.string().parse(value),
  'device-list': value => z.array(z.string()).parse(value),
  tctoken: value => {
    const token = z.object({ token: buffer, timestamp: z.string().optional(), senderTimestamp: z.number().optional() }).parse(value);
    return { token: token.token, ...(token.timestamp === undefined ? {} : { timestamp: token.timestamp }),
      ...(token.senderTimestamp === undefined ? {} : { senderTimestamp: token.senderTimestamp }) };
  },
  'identity-key': value => bytes.parse(value),
};
export function parseKey<K extends keyof SignalDataTypeMap>(category: K, value: unknown): SignalDataTypeMap[K] {
  return parsers[category](value);
}
