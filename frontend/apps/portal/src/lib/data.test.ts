import { describe, expect, it } from 'vitest';
import { homeFor, normaliseSession } from './api';
import { historyOf, toCall, toHelpRequest, type RawRequest } from './data';

const raw = (over: Partial<RawRequest> = {}): RawRequest => ({
  id: 'r1',
  short_id: 'A1B2',
  device_id: 'd1',
  state: 'PENDING',
  label: 'Waiting for your answer',
  flags: {},
  urgency: null,
  incident_type: 'weapon',
  incident_ids: ['i1'],
  evidence_state: 'confirmed',
  trigger_level: 3,
  peak_level: 3,
  created_at: '2026-10-28T10:00:00Z',
  updated_at: '2026-10-28T10:00:05Z',
  expires_at: '2026-10-28T10:30:00Z',
  remaining_s: 1700,
  seen_at: null,
  authority: null,
  sos_text: null,
  parent_request_id: null,
  outcome: null,
  test_mode: true,
  desk_acknowledged_at: null,
  actions: ['approve', 'checking', 'decline'],
  ...over,
});

describe('toHelpRequest', () => {
  it('keeps the server’s actions and reads the evidence', () => {
    const r = toHelpRequest(raw(), { defaultAuthority: 'police' });
    expect(r.actions).toEqual(['approve', 'checking', 'decline']);
    expect(r.recommendedAuthority).toBe('POLICE');
    expect(r.contacts).toEqual([]);
    expect(r.desk).toBeNull();
    expect(toHelpRequest(raw({ flags: { simulated: true } })).evidence).toBe('simulated');
  });

  it('shows who was told once help is asked', () => {
    const r = toHelpRequest(raw({ state: 'APPROVED', flags: { contacts_notified: true }, actions: ['safe'] }), {
      events: [{ seq: 3, at: '2026-10-28T10:01:00Z', event: 'approve', from_state: 'PENDING', to_state: 'APPROVED', actor_type: 'user', channel: 'portal', origin: 'cloud', detail: {} }],
    });
    expect(r.approvedBy).toBe('you');
    expect(r.contacts[0]).toMatchObject({ status: 'Alerted', tone: 'ok' });
    expect(r.desk?.status).toBe('Sending…');
    expect(toHelpRequest(raw({ state: 'ACKNOWLEDGED', desk_acknowledged_at: '2026-10-28T10:02:00Z' })).desk?.status).toBe('Acknowledged');
  });

  it('turns events into history lines and drops the ones nobody needs', () => {
    const lines = historyOf([
      { seq: 1, at: '2026-10-28T10:00:00Z', event: 'created', from_state: null, to_state: 'PENDING', actor_type: 'device', channel: null, origin: 'device', detail: {} },
      { seq: 2, at: '2026-10-28T10:00:01Z', event: 'notification', from_state: null, to_state: null, actor_type: 'system', channel: 'email', origin: 'cloud', detail: {} },
      { seq: 3, at: '2026-10-28T10:00:30Z', event: 'checking', from_state: null, to_state: null, actor_type: 'user', channel: 'portal', origin: 'cloud', detail: {} },
    ]);
    expect(lines.map((l) => l.text)).toEqual(['Alert started', 'You said you are checking']);
  });
});

describe('toCall', () => {
  it('never offers 112 in test mode', () => {
    const c = toCall({ href: 'tel:+911234567890', label: 'Call the SENTRIX Desk (demo)', test_mode: true }, true);
    expect(c.number).toBe('+911234567890');
    expect(c.note).toMatch(/never 112/);
    expect(toCall({ href: null, label: '', test_mode: true }, true)).toMatchObject({ number: null, label: 'Call the SENTRIX Desk' });
  });
});

describe('session', () => {
  it('sends staff to two-factor first and owners into setup', () => {
    const staff = normaliseSession({ signed_in: true, user: { id: 'u', username: 'op', role: 'operator', totp: { enabled: true, verified: false, required: true } }, test_mode: { on: true } });
    expect(homeFor(staff)).toBe('/account/2fa');
    const owner = normaliseSession({ signed_in: true, user: { id: 'u', username: 'o', role: 'owner', totp: {} }, household: { id: 'h', setup_step: 3, setup_done: false, time_zone: 'Asia/Kolkata' }, test_mode: { on: true } });
    expect(homeFor(owner)).toBe('/setup');
    expect(owner.timezone).toBe('Asia/Kolkata');
    expect(normaliseSession({}).testMode).toBe(true);
  });
});
