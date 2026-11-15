import assert from 'node:assert/strict';
import { mkdtemp, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { Zalo } from 'zca-js';
import type { ImageMetadataGetter } from 'zca-js';
import { z } from 'zod';
import { createClient } from '../src/sdk.js';
import { png } from './support.js';

test('production SDK metadata callback reads real PNG dimensions/size and propagates read/decode errors', async context => {
  const client = createClient();
  assert.ok(client instanceof Zalo);
  const options: unknown = Reflect.get(client, 'options');
  const { imageMetadataGetter } = z.object({
    imageMetadataGetter: z.custom<ImageMetadataGetter>(value => typeof value === 'function'),
  }).parse(options);
  const directory = await mkdtemp(join(tmpdir(), 'zalo-image-'));
  context.after(() => rm(directory, { recursive: true, force: true }));
  const imagePath = join(directory, 'tiny.png');
  const data = Buffer.from(png, 'base64');
  await writeFile(imagePath, data);
  assert.deepEqual(await imageMetadataGetter(imagePath), { width: 1, height: 1, size: data.length });
  assert.equal((await stat(imagePath)).size, data.length);
  await assert.rejects(imageMetadataGetter(join(directory, 'missing.png')), { code: 'ENOENT' });
  const malformedPath = join(directory, 'malformed.png');
  await writeFile(malformedPath, 'malformed image bytes');
  await assert.rejects(imageMetadataGetter(malformedPath), /unsupported image format/i);
});
