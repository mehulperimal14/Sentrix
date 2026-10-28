import { createHmac } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { STATE_FILE } from './global-setup';

export interface World {
  dir: string;
  portal: string;
  device: string;
  admin: { username: string; password: string; totp: string };
  desk: { username: string; password: string; totp: string };
  owner: { username: string; password: string | null; activation: string | null };
  sosPin: string | null;
  duressPin: string | null;
  device_password: string;
  claim: string | null;
}

export function world(): World {
  return JSON.parse(readFileSync(STATE_FILE, 'utf-8')) as World;
}

/** RFC 6238 TOTP (SHA-1, 6 digits, 30 s) for the demo's staff secrets. */
export function totp(secret: string, at = Date.now()): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '';
  for (const ch of secret.replace(/=+$/, '').toUpperCase()) bits += alphabet.indexOf(ch).toString(2).padStart(5, '0');
  const bytes = Buffer.from(bits.match(/.{8}/g)!.map((b) => parseInt(b, 2)));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(at / 30_000)));
  const h = createHmac('sha1', bytes).update(counter).digest();
  const off = h[h.length - 1] & 0xf;
  const code = (h.readUInt32BE(off) & 0x7fffffff) % 1_000_000;
  return String(code).padStart(6, '0');
}
