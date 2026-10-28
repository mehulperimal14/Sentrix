import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

/** Starts a fresh test-mode demo world for the end-to-end run:
 *  tools/demo_reset.py in a temp dir (never var/), then tools/demo_run.py
 *  (Portal on portal.localhost:8100, device on 127.0.0.1:8000, a video file as
 *  the camera, every live switch forced off). Credentials are parsed from the
 *  reset's one-time printout and kept in e2e/.world.json (git-ignored). */

export const REPO = resolve(__dirname, '..', '..');
export const PYTHON = process.env.E2E_PYTHON ?? resolve(REPO, '..', '..', '..', '.venv', 'Scripts', 'python.exe');
export const STATE_FILE = join(__dirname, '.world.json');

function grab(text: string, re: RegExp): string {
  const m = text.match(re);
  if (!m) throw new Error(`demo_reset output did not match ${re}`);
  return m[1].trim();
}

async function waitFor(url: string, ms = 120_000) {
  const start = Date.now();
  while (Date.now() - start < ms) {
    try {
      const res = await fetch(url);
      if (res.status < 500) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(`${url} did not come up`);
}

export default async function globalSetup() {
  const dir = mkdtempSync(join(tmpdir(), 'sentrix-e2e-'));
  const reset = spawnSync(PYTHON, ['tools/demo_reset.py', '--dir', dir, ...(process.env.E2E_RESET_ARGS?.split(' ').filter(Boolean) ?? [])], {
    cwd: REPO,
    encoding: 'utf-8',
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  });
  if (reset.status !== 0) throw new Error(`demo_reset failed:\n${reset.stdout}\n${reset.stderr}`);
  const out = reset.stdout;
  const world = {
    dir,
    portal: 'http://portal.localhost:8100',
    device: 'http://127.0.0.1:8000',
    admin: { username: grab(out, /Company admin \((\S+)\)/), password: grab(out, /Company admin[\s\S]*?password\s+(\S+)/), totp: grab(out, /Company admin[\s\S]*?TOTP secret\s+(\S+)/) },
    desk: { username: grab(out, /Desk operator \((\S+)\)/), password: grab(out, /Desk operator[\s\S]*?password\s+(\S+)/), totp: grab(out, /Desk operator[\s\S]*?TOTP secret\s+(\S+)/) },
    customer: grab(out, /^Customer (.+?) \(\S+\)\r?$/m),
    serial: grab(out, /^Device (\S+) \(/m),
    deviceId: grab(out, /^Device \S+ \((\S+)\)/m),
    owner: { username: grab(out, /^Customer .+? \((\S+)\)\r?$/m), password: (out.match(/Customer[\s\S]*?password\s+(\S+)/) ?? [])[1] ?? null, activation: (out.match(/activation\s+([A-Z0-9-]{8,})/) ?? [])[1] ?? null },
    sosPin: (out.match(/SOS PIN\s+(\d{6})/) ?? [])[1] ?? null,
    duressPin: (out.match(/[Dd]uress PIN\s+(\d{6})/) ?? [])[1] ?? null,
    device_password: grab(out, /Local Console password \(device admin\)\s+(\S+)/),
    claim: (out.match(/claim code\s+([A-Z0-9-]+)/) ?? [])[1] ?? null,
    pid: 0,
  };
  const run = spawn(PYTHON, ['tools/demo_run.py', '--dir', dir, 'start'], {
    cwd: REPO,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    stdio: 'ignore',
    detached: false,
    windowsHide: true,
  });
  world.pid = run.pid ?? 0;
  writeFileSync(STATE_FILE, JSON.stringify(world, null, 2));
  await waitFor(`${world.portal}/healthz`);
  await waitFor(`${world.device}/api/session`);
}
