import { expect, portalSignIn, test } from './fixtures';

/* The household's everyday journeys on the Portal app (Experience Plan,
   "Everyday"), on a laptop and on a phone. */

test('signed out, an app page asks to sign in and returns there @phone', async ({ page, w, errors }) => {
  await page.goto(`${w.portal}/people`);
  await expect(page).toHaveURL(/\/login\?next=%2Fpeople/);
  await page.getByLabel('Username').fill(w.owner.username);
  await page.getByLabel('Password').fill(w.owner.password!);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page).toHaveURL(/\/people$/);
  await expect(page.getByRole('heading', { name: 'Emergency contacts' })).toBeVisible();
  // The owner's face gallery comes from the device through the tunnel.
  await expect(page.getByRole('heading', { name: 'Faces SENTRIX recognises' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Upload photos' })).toBeVisible();
  void errors;
});

test('a wrong password is refused with a message', async ({ page, w, errors }) => {
  await page.goto(`${w.portal}/login`);
  // An unknown name: a wrong password for the demo owner would start the
  // account's sign-in back-off and lock the other tests out.
  await page.getByLabel('Username').fill('nobody_e2e');
  await page.getByLabel('Password').fill('not-the-password');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('alert').or(page.getByText(/password/i).nth(1))).toBeVisible();
  await expect(page).toHaveURL(/\/login/);
  void errors;
});

test('home says whether the home is OK and shows the device @phone', async ({ page, w, errors }) => {
  await portalSignIn(page, w, 'owner');
  await expect(page).toHaveURL(/\/home$/);
  await expect(page.getByText(/All clear|Something unusual|Someone we don’t recognise|is offline/).first()).toBeVisible();
  await expect(page.getByRole('radiogroup', { name: 'Mode' }).or(page.getByRole('group', { name: 'Mode' }))).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Today' })).toBeVisible();
  void errors;
});

test('the four sections are reachable from the navigation @phone', async ({ page, w, errors }) => {
  await portalSignIn(page, w, 'owner');
  const nav = page.getByRole('navigation', { name: 'Sections' }).locator('visible=true');
  await nav.getByRole('link', { name: 'Activity' }).click();
  await expect(page.getByRole('heading', { name: 'Activity' })).toBeVisible();
  await nav.getByRole('link', { name: 'People' }).click();
  await expect(page.getByRole('heading', { name: 'Who lives here' })).toBeVisible();
  await nav.getByRole('link', { name: 'Settings' }).click();
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
  await nav.getByRole('link', { name: 'Home' }).click();
  await expect(page).toHaveURL(/\/home$/);
  void errors;
});

test('every settings page loads with the saved values', async ({ page, w, errors }) => {
  await portalSignIn(page, w, 'owner');
  await page.goto(`${w.portal}/settings/alerts`);
  await expect(page.getByRole('heading', { name: 'Alerts and PINs' })).toBeVisible();
  await expect(page.getByText('SOS PIN', { exact: true })).toBeVisible();
  await page.goto(`${w.portal}/settings/home`);
  await expect(page.getByLabel('City')).not.toHaveValue('');
  await page.goto(`${w.portal}/settings/devices`);
  await expect(page.getByText(`Serial ${w.serial}`, { exact: false })).toBeVisible();
  await page.goto(`${w.portal}/settings/account`);
  await expect(page.getByText(w.owner.username)).toBeVisible();
  await expect(page.getByText('This browser')).toBeVisible();
  await page.goto(`${w.portal}/settings/privacy`);
  await expect(page.getByRole('heading', { name: 'Your choices' })).toBeVisible();
  void errors;
});

test('saving the home address keeps it', async ({ page, w, errors }) => {
  await portalSignIn(page, w, 'owner');
  await page.goto(`${w.portal}/settings/home`);
  const landmark = page.getByLabel('Landmark');
  const before = await landmark.inputValue();
  await landmark.fill('Opposite the e2e test ATM');
  await page.getByRole('button', { name: 'Save home details' }).click();
  await expect(page.getByText(/Home details saved/)).toBeVisible();
  await page.reload();
  await expect(page.getByLabel('Landmark')).toHaveValue('Opposite the e2e test ATM');
  await page.getByLabel('Landmark').fill(before);
  await page.getByRole('button', { name: 'Save home details' }).click();
  await expect(page.getByText(/Home details saved/)).toBeVisible();
  void errors;
});

test('the setup wizard opens on saved answers and saves them again', async ({ page, w, errors }) => {
  await portalSignIn(page, w, 'owner');
  await page.goto(`${w.portal}/setup/you`);
  await expect(page.getByText('Step 1 of 5: You and your address')).toBeVisible();
  await expect(page.getByLabel('Your name')).not.toHaveValue('');
  await page.getByRole('button', { name: 'Save and continue' }).click();
  await expect(page).toHaveURL(/\/setup\/people$/);
  await expect(page.getByRole('heading', { name: 'Emergency contacts' })).toBeVisible();
  void errors;
});

test('a contact can be added and gets a one-time link', async ({ page, w, errors }) => {
  await portalSignIn(page, w, 'owner');
  await page.goto(`${w.portal}/people`);
  const add = page.getByRole('button', { name: 'Add an emergency contact' });
  test.skip(!(await add.isVisible()), 'the demo household already has the most contacts allowed');
  await add.click();
  await page.getByLabel('Name').fill('E2E Neighbour');
  await page.getByLabel('Mobile number').fill('+919812345670');
  await page.getByRole('button', { name: 'Add contact' }).click();
  await expect(page.getByRole('heading', { name: 'Ask E2E Neighbour to confirm' })).toBeVisible();
  const link = await page.getByLabel('Confirmation link').inputValue();
  expect(link).toMatch(/\/consent\//);
  await page.getByRole('button', { name: 'Done' }).click();
  await expect(page.getByText('E2E Neighbour')).toBeVisible();
  await expect(page.getByText('Waiting to confirm').first()).toBeVisible();

  // The contact opens the link without an account and confirms.
  const contact = await page.context().browser()!.newContext();
  const cp = await contact.newPage();
  await cp.goto(link);
  await expect(cp.getByRole('heading', { name: /added you as an emergency contact/ })).toBeVisible();
  await cp.getByRole('button', { name: 'Yes, alert me' }).click();
  await expect(cp.getByRole('heading', { name: 'Thank you' })).toBeVisible();
  await contact.close();

  await page.reload();
  const row = page.getByRole('button', { name: /E2E Neighbour/ });
  await expect(row.getByText('Confirmed')).toBeVisible();
  await row.click();
  await page.getByRole('button', { name: 'Remove E2E Neighbour' }).click();
  await expect(page.getByText('E2E Neighbour removed')).toBeVisible();
  void errors;
});

test('old Portal addresses still land on the right page', async ({ page, w, errors }) => {
  await portalSignIn(page, w, 'owner');
  for (const [from, to] of [
    ['/household/contacts', '/people'],
    ['/household', '/settings/home'],
    ['/privacy', '/settings/privacy'],
    ['/onboarding', '/setup'],
    ['/account', '/settings/account'],
  ]) {
    await page.goto(`${w.portal}${from}`);
    await expect(page).toHaveURL(new RegExp(`${to.replace(/\//g, '\\/')}(\\/[a-z]+)?$`));
  }
  void errors;
});

test('an unknown page says so, and an unknown API path is JSON', async ({ page, w, request, errors }) => {
  await page.goto(`${w.portal}/no-such-page`);
  await expect(page.getByText('This page doesn’t exist')).toBeVisible();
  const res = await request.get(`http://127.0.0.1:8100/api/v2/no-such-thing`);
  expect(res.status()).toBe(404);
  expect(res.headers()['content-type']).toContain('application/json');
  void errors;
});

test('a closed contact link explains itself @phone', async ({ page, w, errors }) => {
  await page.goto(`${w.portal}/c/not-a-real-token`);
  await expect(page.getByText('This alert page has closed')).toBeVisible();
  await page.goto(`${w.portal}/consent/not-a-real-token`);
  await expect(page.getByText('This link no longer works')).toBeVisible();
  void errors;
});
