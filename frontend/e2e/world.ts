import { createHmac } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { PYTHON, REPO, STATE_FILE } from './global-setup';

export interface World {
  dir: string;
  portal: string;
  device: string;
  admin: { username: string; password: string; totp: string };
  desk: { username: string; password: string; totp: string };
  customer: string;
  serial: string;
  deviceId: string;
  owner: { username: string; password: string | null; activation: string | null };
  sosPin: string | null;
  duressPin: string | null;
  device_password: string;
  claim: string | null;
}

/** Runs tools/demo_run.py against the running world (e.g. `trigger weapon`). */
export function demo(...args: string[]): string {
  const w = world();
  const r = spawnSync(PYTHON, ['tools/demo_run.py', '--dir', w.dir, ...args], { cwd: REPO, encoding: 'utf-8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
  if (r.status !== 0) throw new Error(`demo_run ${args.join(' ')} failed:
${r.stdout}
${r.stderr}`);
  return r.stdout;
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
