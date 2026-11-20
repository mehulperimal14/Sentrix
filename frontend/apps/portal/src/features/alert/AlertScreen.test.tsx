import { ToastProvider } from '@sentrix/ui';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { HelpRequest } from '../../lib/models';
import { AlertScreen } from './AlertScreen';

const base: HelpRequest = {
  id: 'r1',
  shortId: 'A1B2',
  state: 'PENDING',
  label: 'Waiting for your answer',
  incidentType: 'weapon',
  level: 3,
  evidence: 'confirmed',
  createdAt: new Date().toISOString(),
  expiresAt: new Date(Date.now() + 1_800_000).toISOString(),
  checkingUntil: null,
  room: 'Living room',
  snapshotUrl: null,
  actions: ['approve', 'checking', 'decline'],
  authority: null,
  recommendedAuthority: 'POLICE',
  deviceId: 'd1',
  linked: false,
  approvedAt: null,
  approvedBy: null,
  contacts: [],
  desk: null,
  safetyNote: null,
  sosText: null,
  history: [],
  outcome: null,
  residentSafe: false,
};
const call = { label: 'Call the SENTRIX Desk', number: null, note: 'Calling is off in this demo.' };

function show(r: Partial<HelpRequest> = {}) {
  return render(
    <ToastProvider>
      <AlertScreen request={{ ...base, ...r }} call={call} timezone="Asia/Kolkata" answer={vi.fn()} />
    </ToastProvider>,
  );
}

describe('AlertScreen', () => {
  it('asks the question with the SOS contract’s words', () => {
    show();
    expect(screen.getByRole('heading', { name: 'Possible weapon in the Living room' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Alert my contacts and the SENTRIX desk/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /I'm checking/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'False alarm (PIN)' })).toBeInTheDocument();
    expect(screen.getAllByText(/SENTRIX does not contact 112 itself\./).length).toBeGreaterThan(0);
    expect(screen.getByText(/Possible \(one camera check\)/)).toBeInTheDocument();
  });

  it('calls a robbery request a possible theft (D26)', () => {
    show({ incidentType: 'robbery', room: 'Hall' });
    expect(screen.getByRole('heading', { name: 'Possible theft in the Hall' })).toBeInTheDocument();
  });

  it('offers only what the server allows', () => {
    show({ actions: ['checking'] });
    expect(screen.queryByRole('button', { name: /Alert my contacts/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'False alarm (PIN)' })).toBeNull();
  });

  it('says when two models agreed', () => {
    show({ evidence: 'corroborated' });
    expect(screen.getByRole('heading', { name: 'Weapon confirmed in the Living room' })).toBeInTheDocument();
    expect(screen.getByText(/Confirmed by two different models/)).toBeInTheDocument();
  });

  it('never links to 112 in test mode', () => {
    const { container } = show({ state: 'APPROVED', actions: ['safe'], approvedAt: new Date().toISOString(), approvedBy: 'you' });
    expect(container.querySelector('a[href^="tel:112"]')).toBeNull();
  });
});

