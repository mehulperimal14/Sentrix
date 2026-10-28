import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync, rmSync } from 'node:fs';
import { STATE_FILE } from './global-setup';

/** Stops the demo world (the whole process tree) and removes its temp dir. */
export default async function globalTeardown() {
  if (!existsSync(STATE_FILE)) return;
  const world = JSON.parse(readFileSync(STATE_FILE, 'utf-8'));
  if (world.pid) {
    if (process.platform === 'win32') spawnSync('taskkill', ['/PID', String(world.pid), '/T', '/F']);
    else {
      try {
        process.kill(world.pid, 'SIGINT');
      } catch {
        /* already gone */
      }
    }
  }
  await new Promise((r) => setTimeout(r, 1500));
  try {
    rmSync(world.dir, { recursive: true, force: true });
  } catch {
    /* Windows may keep a file open for a moment; the temp dir is harmless */
  }
  rmSync(STATE_FILE, { force: true });
}
