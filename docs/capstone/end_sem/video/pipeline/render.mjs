// Frame-accurate renderer: each job is an HTML page whose CSS animations are paused and
// stepped to t = frame / fps; a frame is captured only when some animation's progress
// changed (otherwise the last JPEG is written again), and frames are piped into ffmpeg.
//
//   node render.mjs jobs.json [concurrency]
//   jobs.json: [{ "html": "C:/.../scene.html", "dur": 12.4, "out": "C:/.../scene.mp4" }, ...]
import { chromium } from 'file:///G:/Sentrix/.claude/worktrees/frontend-v2/frontend/node_modules/playwright-core/index.mjs';
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const FPS = 30;
const jobs = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const conc = Number(process.argv[3] || 4);
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';

const browser = await chromium.launch({ executablePath: EDGE, headless: true,
  args: ['--force-color-profile=srgb', '--disable-lcd-text', '--font-render-hinting=none', '--hide-scrollbars'] });

async function renderJob(job) {
  const t0 = Date.now();
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  await page.goto(pathToFileURL(job.html).href, { waitUntil: 'load' });
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all([...document.images].map(i => i.complete ? null : new Promise(r => { i.onload = i.onerror = r; })));
    document.getAnimations().forEach(a => a.pause());
  });
  const cdp = await ctx.newCDPSession(page);
  const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-c:v', 'mjpeg',
    '-i', '-', '-c:v', 'libx264', '-preset', 'medium', '-crf', '14', '-pix_fmt', 'yuv420p', '-r', String(FPS), job.out],
    { stdio: ['pipe', 'inherit', 'inherit'] });
  const done = new Promise((res, rej) => ff.on('close', c => c === 0 ? res() : rej(new Error(`ffmpeg ${c} for ${job.out}`))));
  const n = Math.round(job.dur * FPS);
  let last = null, lastSig = null, shots = 0;
  for (let f = 0; f < n; f++) {
    const sig = await page.evaluate(ms => {
      const parts = [];
      for (const a of document.getAnimations()) {
        a.currentTime = ms;
        const p = a.effect.getComputedTiming().progress;
        parts.push(p === null ? -1 : Math.round(p * 1e4));
      }
      return parts.join(',');
    }, (f / FPS) * 1000);
    if (sig !== lastSig || last === null) {
      const { data } = await cdp.send('Page.captureScreenshot', { format: 'jpeg', quality: 94, optimizeForSpeed: false });
      last = Buffer.from(data, 'base64');
      lastSig = sig;
      shots++;
    }
    if (!ff.stdin.write(last)) await new Promise(r => ff.stdin.once('drain', r));
  }
  ff.stdin.end();
  await done;
  await ctx.close();
  console.log(`${job.out.split(/[\\/]/).pop()}  ${n} frames, ${shots} captured, ${((Date.now() - t0) / 1000).toFixed(1)} s`);
}

const queue = [...jobs];
await Promise.all(Array.from({ length: Math.min(conc, queue.length) }, async () => {
  while (queue.length) await renderJob(queue.shift());
}));
await browser.close();
