import { open } from 'node:fs/promises';
import { StringDecoder } from 'node:string_decoder';

const characterLimit = 250_000;

export async function readBoundedText(input) {
  const decoder = new StringDecoder('utf8');
  const buffer = Buffer.allocUnsafe(16 * 1024);
  const file = await open(input, 'r');
  let text = '';
  try {
    while (true) {
      const remaining = characterLimit - text.length;
      const { bytesRead } = await file.read(buffer, 0, Math.min(buffer.length, remaining + 1), null);
      const chunk = bytesRead ? decoder.write(buffer.subarray(0, bytesRead)) : decoder.end();
      if (chunk.length > remaining) {
        throw new Error('Input exceeds 250,000 characters; select a smaller section');
      }
      text += chunk;
      if (bytesRead === 0) return text;
    }
  } finally {
    await file.close();
  }
}
