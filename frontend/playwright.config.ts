import { defineConfig, devices } from '@playwright/test';

/** End-to-end tests run against the real Portal and device apps in a test-mode
 *  demo world (e2e/global-setup.ts). Edge is used so no browser download is
 *  needed on Windows; set E2E_CHANNEL=chrome or chromium to change it. */
const channel = process.env.E2E_CHANNEL ?? 'msedge';

export default defineConfig({
  testDir: './e2e',
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  globalSetup: './e2e/global-setup.ts',
  globalTeardown: './e2e/global-teardown.ts',
  use: {
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    channel,
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Edge'], channel, viewport: { width: 1280, height: 800 } } },
    { name: 'phone', use: { ...devices['Pixel 7'], channel, viewport: { width: 375, height: 812 } }, grep: /@phone/ },
  ],
});
