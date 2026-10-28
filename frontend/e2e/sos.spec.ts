import { expect, portalSignIn, test } from './fixtures';
import { demo } from './world';

/* The alert journey end to end, in test mode: a scripted (simulated) weapon
   detection on the device → the owner's question on the Portal → help asked
   → the SENTRIX Desk acknowledges and closes it. Nothing leaves the machine:
   demo_run forces every live switch off and blanks the provider keys. */

test.describe.configure({ mode: 'serial' });

test('the owner is asked, answers, and the Desk handles it', async ({ page, browser, w, errors }) => {
  await portalSignIn(page, w, 'owner');
  await expect(page).toHaveURL(/\/home$/);

  demo('trigger', 'weapon');

  // The red bar reaches every page; it opens the alert.
  const bar = page.getByRole('alert').filter({ hasText: 'Needs your answer' });
  await expect(bar).toBeVisible({ timeout: 30_000 });
  await bar.getByRole('link', { name: 'Answer' }).click();
  await expect(page).toHaveURL(/\/alert\//);
  await expect(page.getByRole('button', { name: 'Alert my contacts and the SENTRIX desk' })).toBeVisible();
  await expect(page.getByText('SENTRIX does not contact 112 itself.').first()).toBeVisible();
  // Test mode never offers a call to 112.
  await expect(page.locator('a[href^="tel:112"]')).toHaveCount(0);

  await page.getByRole('button', { name: 'I’m checking' }).or(page.getByRole('button', { name: "I'm checking" })).click();
  await expect(page.getByText(/Reminders paused until/)).toBeVisible();

  await page.getByRole('radio', { name: 'Police' }).or(page.getByRole('button', { name: 'Police' })).first().click();
  await page.getByRole('button', { name: 'Alert my contacts and the SENTRIX desk' }).click();
  await expect(page.getByRole('heading', { name: /alerting your contacts and the SENTRIX Desk|Desk is handling it/ })).toBeVisible();
  const alertUrl = page.url();

  // The Desk (simulated responder console) sees and acknowledges it.
  const deskCtx = await browser.newContext();
  const desk = await deskCtx.newPage();
  await portalSignIn(desk, w, 'desk');
  await expect(desk).toHaveURL(/\/desk$/);
  await expect(desk.getByText(/waiting for an operator/)).toBeVisible();
  await desk.getByRole('row').nth(1).click();
  await expect(desk).toHaveURL(/\/desk\/.+/);
  await desk.getByRole('button', { name: 'Acknowledge' }).click();
  await expect(page.getByRole('heading', { name: 'The SENTRIX Desk is handling it' })).toBeVisible({ timeout: 20_000 });

  await desk.getByLabel('Outcome').selectOption({ index: 1 });
  await desk.getByRole('button', { name: 'Close the request' }).click();
  await expect(desk.getByText(/^Closed/)).toBeVisible();
  await deskCtx.close();

  await page.goto(alertUrl);
  await expect(page.getByText('Closed').first()).toBeVisible();
  await page.goto(`${w.portal}/activity`);
  await expect(page.getByRole('heading', { name: 'Help requests' })).toBeVisible();
  void errors;
});

test('a false alarm needs the SOS PIN, and a wrong PIN is refused @phone', async ({ page, w, errors }) => {
  test.skip(!w.sosPin, 'the demo world has no SOS PIN');
  await portalSignIn(page, w, 'owner');
  demo('trigger', 'fire');
  const bar = page.getByRole('alert').filter({ hasText: 'Needs your answer' });
  await expect(bar).toBeVisible({ timeout: 30_000 });
  await bar.getByRole('link', { name: 'Answer' }).click();
  await page.getByRole('button', { name: 'False alarm (PIN)' }).click();
  const pad = page.getByRole('dialog');
  await pad.locator('input').first().focus();
  await page.keyboard.type('000000');
  await expect(pad.getByText(/wrong|PIN/i).first()).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'False alarm (PIN)' }).click();
  await page.getByRole('dialog').locator('input').first().focus();
  await page.keyboard.type(w.sosPin!);
  await expect(page.getByText(/false alarm/i).first()).toBeVisible();
  await expect(page.getByRole('alert').filter({ hasText: 'Needs your answer' })).toHaveCount(0);
  void errors;
});
