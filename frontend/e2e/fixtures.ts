import { expect, test as base, type Page } from '@playwright/test';
import { totp, world, type World } from './world';

/** Every test fails on a console error or a CSP violation in the page. */
export const test = base.extend<{ w: World; errors: string[] }>({
  w: async ({}, use) => use(world()),
  errors: async ({ page }, use) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() !== 'error') return;
      const text = m.text();
      // A deliberately refused request (401 before sign-in, 404 of a bad
      // link) is logged by the browser; the app shows those itself.
      if (/Failed to load resource: the server responded with a status of (401|403|404|409|422|423)/.test(text)) return;
      errors.push(text);
    });
    page.on('pageerror', (e) => errors.push(String(e)));
    await use(errors);
    expect(errors, 'console errors').toEqual([]);
  },
});
export { expect };

export async function portalSignIn(page: Page, w: World, who: 'owner' | 'desk' | 'admin') {
  const creds = who === 'owner' ? { username: w.owner.username, password: w.owner.password! } : w[who];
  await page.goto(`${w.portal}/login`);
  await page.getByLabel('Username').fill(creds.username);
  await page.getByLabel('Password').fill(creds.password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  if (who !== 'owner') {
    await expect(page.getByRole('heading', { name: 'Enter your authenticator code' })).toBeVisible();
    // A TOTP code is accepted once: wait for a fresh 30 s window when the
    // other staff test used this one.
    await page.getByLabel('Code').fill(totp((w as World)[who].totp));
    await page.getByRole('button', { name: 'Continue' }).click();
  }
  // Signed in once the app has left the sign-in pages.
  await expect(page).not.toHaveURL(/\/(login|account\/2fa)/, { timeout: 20_000 });
}

export async function deviceSignIn(page: Page, w: World) {
  await page.goto(`${w.device}/login`);
  await page.getByLabel('Device password').fill(w.device_password);
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 20_000 });
}
