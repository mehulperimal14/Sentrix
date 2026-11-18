import { deviceSignIn, expect, portalSignIn, test } from './fixtures';

/* The company admin console, and the device's own console on the LAN. */

test('the admin console lists the demo customer and device', async ({ page, w, errors }) => {
  // A staff TOTP code works once: start in a fresh 30 s window so the Desk
  // sign-in of the SOS test cannot have used this one.
  await page.waitForTimeout(30_000 - (Date.now() % 30_000) + 500);
  await portalSignIn(page, w, 'admin');
  await expect(page).toHaveURL(/\/admin$/);
  await expect(page.getByText('Customers', { exact: true })).toBeVisible();
  await expect(page.getByText(/audit log is intact/)).toBeVisible();
  await page.getByRole('link', { name: 'Customers' }).first().click();
  await page.getByRole('link', { name: w.customer }).click();
  await expect(page.getByRole('heading', { name: w.customer })).toBeVisible();
  await expect(page.getByText(w.serial)).toBeVisible();
  await page.getByRole('navigation', { name: 'Sections' }).getByRole('link', { name: 'Devices' }).click();
  await expect(page.getByRole('cell', { name: w.serial })).toBeVisible();
  await page.getByRole('navigation', { name: 'Sections' }).getByRole('link', { name: 'Staff' }).click();
  await expect(page.getByRole('cell', { name: w.desk.username })).toBeVisible();
  await page.getByRole('navigation', { name: 'Sections' }).getByRole('link', { name: 'Audit log' }).click();
  await expect(page.getByRole('heading', { name: 'Audit log' })).toBeVisible();
  void errors;
});

test('the device console signs in and shows the wall view', async ({ page, w, errors }) => {
  await deviceSignIn(page, w);
  await expect(page).toHaveURL(new RegExp(`${w.device.replace(/[./]/g, '\\$&')}/?$`));
  // The wall view, or the alert takeover if an alert from another test is open.
  await expect(page.getByText(/camera|Needs an answer|Help requested/).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('Call 112 now')).toHaveCount(0);
  void errors;
});

test('every device admin page loads', async ({ page, w, errors }) => {
  await deviceSignIn(page, w);
  for (const [path, heading] of [
    ['diagnostics', 'Diagnostics'],
    ['activity', 'Activity'],
    ['sos', 'Help requests'],
    ['recordings', 'Record video'],
    ['faces', 'Faces'],
    ['hardware', 'Hardware'],
    ['network', 'Network'],
  ]) {
    await page.goto(`${w.device}/admin/${path}`);
    await expect(page.getByRole('heading', { name: heading, level: 1 })).toBeVisible();
  }
  void errors;
});

test('old device console addresses land on the new pages', async ({ page, w, errors }) => {
  await deviceSignIn(page, w);
  for (const [from, to] of [
    ['/events', '/admin/activity'],
    ['/node', '/admin/hardware'],
    ['/authorized', '/admin/faces'],
    ['/dashboard', '/'],
  ]) {
    await page.goto(`${w.device}${from}`);
    await expect(page).toHaveURL(new RegExp(`${to.replace(/\//g, '\\/')}$`));
  }
  void errors;
});

test('the Portal shows the device page through the tunnel only as data', async ({ request, w }) => {
  // The device app's shell is never served through the Portal tunnel.
  const res = await request.get(`http://127.0.0.1:8100/d/${w.deviceId}/admin/diagnostics`, { maxRedirects: 0 });
  expect([302, 303, 307, 401, 403, 404]).toContain(res.status());
});
