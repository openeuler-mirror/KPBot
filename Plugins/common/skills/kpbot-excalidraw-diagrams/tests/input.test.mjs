import { after, test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, open, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { readBoundedText } from '../scripts/input.mjs';

const directory = await mkdtemp(path.join(tmpdir(), 'kpbot-diagram-input-'));
after(() => rm(directory, { recursive: true, force: true }));
const limit = 250_000;

test('decodes split UTF-8 sequences, BOM and incomplete tails like readFile with utf8', async () => {
  const file = path.join(directory, 'split.mmd');
  // Place the four-byte emoji across the 16 KiB read boundary.
  const prefix = '\uFEFF' + 'a'.repeat(16 * 1024 - 4);
  const bytes = Buffer.concat([Buffer.from(prefix + '🙂图'), Buffer.from([0xe2, 0x82])]);
  await writeFile(file, bytes);
  assert.equal(await readBoundedText(file), bytes.toString('utf8'));
});

test('accepts exactly 250,000 UTF-16 code units even for multibyte input', async () => {
  const file = path.join(directory, 'boundary.md');
  const text = '图'.repeat(limit - 2) + '🙂';
  await writeFile(file, text);
  assert.equal(await readBoundedText(file), text);

  await writeFile(file, '图' + text);
  await assert.rejects(readBoundedText(file), /exceeds 250,000 characters/);
});

test('counts the replacement character emitted at EOF', async () => {
  const file = path.join(directory, 'tail.json');
  await writeFile(file, Buffer.concat([Buffer.alloc(limit, 0x61), Buffer.from([0xe2])]));
  await assert.rejects(readBoundedText(file), /exceeds 250,000 characters/);
});

test('stops reading an oversized file at the first excess ASCII character and closes it', async (t) => {
  const file = path.join(directory, 'large.txt');
  const writer = await open(file, 'w');
  try {
    await writer.truncate(8 * 1024 * 1024);
  } finally {
    await writer.close();
  }
  const prototype = Object.getPrototypeOf(writer);
  const originalRead = prototype.read;
  let bytesRead = 0;
  let reader;
  t.mock.method(prototype, 'read', async function (...args) {
    reader = this;
    const result = await originalRead.apply(this, args);
    bytesRead += result.bytesRead;
    return result;
  });
  await assert.rejects(readBoundedText(file), /exceeds 250,000 characters/);
  assert.equal(bytesRead, limit + 1);
  assert.equal(reader.fd, -1);
});

test('propagates open and read errors and closes the input after a read failure', async (t) => {
  const file = path.join(directory, 'initially-missing.mermaid');
  await assert.rejects(readBoundedText(file), { code: 'ENOENT' });
  const writer = await open(file, 'w');
  await writer.close();
  const failure = new Error('injected read failure');
  let reader;
  t.mock.method(Object.getPrototypeOf(writer), 'read', async function () {
    reader = this;
    throw failure;
  });
  await assert.rejects(readBoundedText(file), error => error === failure);
  assert.equal(reader.fd, -1);
});
