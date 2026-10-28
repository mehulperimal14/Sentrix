import { useQuery } from '@tanstack/react-query';
import { api } from './api';
import type { AnswerAction, Authority, CallInfo, DeviceCard, HelpRequest, RequestState } from './models';

/* ------------------------------------------------------------ raw shapes */
/** OwnerRequest (sentrix_portal/routes/api_v2/schemas.py). */
export interface RawRequest {
  id: string;
  short_id: string;
  device_id: string | null;
  state: string;
  label: string;
  flags: Record<string, any>;
  urgency: string | null;
  incident_type: string | null;
  incident_ids: string[];
  evidence_state: string | null;
  trigger_level: number | null;
  peak_level: number | null;
  created_at: string | null;
  updated_at: string | null;
  expires_at: string | null;
  remaining_s: number | null;
  seen_at: string | null;
  authority: string | null;
  sos_text: string | null;
  parent_request_id: string | null;
  outcome: string | null;
  test_mode: boolean;
  desk_acknowledged_at: string | null;
  actions: string[];
}
export interface RawEvent {
  seq: number;
  at: string | null;
  event: string;
  from_state: string | null;
  to_state: string | null;
  actor_type: string | null;
  channel: string | null;
  origin: string | null;
  detail: Record<string, any>;
}
export interface RawDevice {
  id: string;
  serial: string;
  name: string;
  state: string;
  claimed: boolean;
  online: boolean;
  armed: boolean;
  ai_hz: number | null;
  last_seen: string | null;
  room: string | null;
  last_incident: Record<string, any> | null;
  requests: RawRequest[];
}
export interface HomeOut {
  household_id: string;
  name: string | null;
  erasure_due: string | null;
  setup_done: boolean;
  next_step: number;
  steps_done: Record<string, boolean>;
  requests: RawRequest[];
  devices: RawDevice[];
}
export interface SosDetailOut {
  request: RawRequest;
  events: RawEvent[];
  has_snapshot: boolean;
  snapshot_url: string | null;
  call: { href: string | null; label: string; test_mode: boolean };
  authorities: { value: string; label: string }[];
  default_authority: string;
  footer: string;
}

/* ------------------------------------------------------------ mapping */
const EVENT_TEXT: Record<string, (e: RawEvent) => string | null> = {
  created: () => 'Alert started',
  prompted: () => 'You were asked',
  seen: () => 'You opened the alert',
  notification: () => null,
  approve: (e) => (e.actor_type === 'contact' ? 'Your contact asked the SENTRIX Desk for help' : 'Help requested'),
  checking: () => 'You said you are checking',
  decline: () => 'Marked as false alarm',
  reminder: () => 'Reminder sent',
  lapse_warning: () => 'Lapse warning sent',
  expire: () => 'Unanswered: the answer window ended',
  contacts_notified: () => 'Your contacts were alerted',
  delivered: () => 'Help request delivered',
  delivery_failed: () => 'Not delivered',
  undelivered: () => 'Not delivered yet',
  acknowledge: () => 'Acknowledged by the SENTRIX Desk',
  resolve: () => 'Closed by the SENTRIX Desk',
  stale_close: () => 'Closed automatically after 24 hours',
  level_rise: (e) => `The alert rose to level ${e.detail?.level ?? e.detail?.to ?? ''}`.trim(),
  resident_safe: () => 'You said you are safe',
  help_requested_later: () => 'You asked for help after it ended',
  pin_failed: () => 'A wrong PIN was entered',
  device_offline: () => 'The device went offline',
  device_online: () => 'The device is back online',
  cancel: () => 'Cancelled',
  repage: () => 'The SENTRIX Desk and contacts were reminded',
  attached: () => 'Another detection joined this alert',
};

export function historyOf(events: RawEvent[]): { at: string; text: string }[] {
  const out: { at: string; text: string }[] = [];
  for (const e of events) {
    const f = EVENT_TEXT[e.event];
    const text = f ? f(e) : null;
    if (text && e.at) out.push({ at: e.at, text });
  }
  return out;
}

const AUTH = ['POLICE', 'FIRE', 'MEDICAL'];
function asAuthority(v: string | null | undefined): Authority | null {
  const up = (v ?? '').toUpperCase();
  return AUTH.includes(up) ? (up as Authority) : null;
}

export function toHelpRequest(r: RawRequest, opts: { events?: RawEvent[]; snapshotUrl?: string | null; room?: string | null; defaultAuthority?: string | null } = {}): HelpRequest {
  const events = opts.events ?? [];
  const flags = r.flags ?? {};
  const contactsAlerted = Boolean(flags.contacts_notified) || events.some((e) => e.event === 'contacts_notified');
  const approveEvent = events.find((e) => e.event === 'approve' || e.to_state === 'APPROVED');
  const state = r.state as RequestState;
  const onHelp = ['APPROVED', 'DISPATCHED', 'ACKNOWLEDGED', 'FAILED', 'CLOSED'].includes(state);
  const checkingEvent = [...events].reverse().find((e) => e.event === 'checking');
  const checkingUntil = checkingEvent?.at ? new Date(Date.parse(checkingEvent.at) + 300_000).toISOString() : null;
  return {
    id: r.id,
    shortId: r.short_id,
    state,
    label: r.label,
    incidentType: r.incident_type ?? 'threat',
    level: r.peak_level ?? r.trigger_level,
    evidence: flags.simulated ? 'simulated' : flags.camera_degraded ? 'degraded' : r.evidence_state,
    createdAt: r.created_at ?? new Date().toISOString(),
    expiresAt: r.expires_at,
    checkingUntil,
    room: opts.room ?? null,
    snapshotUrl: opts.snapshotUrl ?? null,
    actions: r.actions as AnswerAction[],
    authority: asAuthority(r.authority),
    recommendedAuthority: asAuthority(opts.defaultAuthority),
    deviceId: r.device_id,
    linked: Boolean(r.parent_request_id),
    approvedAt: approveEvent?.at ?? (onHelp ? r.updated_at : null),
    approvedBy: approveEvent?.actor_type === 'contact' ? 'Your contact' : approveEvent ? 'you' : null,
    contacts: onHelp || contactsAlerted ? [{ name: 'Your contacts', status: contactsAlerted ? 'Alerted' : state === 'FAILED' ? 'Not delivered' : 'Being alerted', tone: contactsAlerted ? 'ok' : state === 'FAILED' ? 'bad' : 'warn' }] : [],
    desk: onHelp
      ? r.desk_acknowledged_at
        ? { status: 'Acknowledged', tone: 'ok' }
        : state === 'FAILED'
          ? { status: 'Not delivered', tone: 'bad' }
          : state === 'APPROVED'
            ? { status: 'Sending…', tone: 'warn' }
            : state === 'CLOSED'
              ? { status: 'Closed', tone: 'ok' }
              : { status: 'Received, waiting for an operator' }
      : null,
    safetyNote: contactsAlerted && state === 'PENDING' ? 'Your contacts were alerted by the safety net and can alert the SENTRIX Desk too.' : null,
    sosText: r.sos_text,
    history: historyOf(events),
    outcome: r.outcome,
    residentSafe: Boolean(flags.resident_safe),
  };
}

export function toCall(c: SosDetailOut['call'] | undefined, testMode: boolean): CallInfo {
  if (!c) return { label: testMode ? 'Call the SENTRIX Desk' : 'Call 112 now', number: null, note: null };
  const number = c.href ? c.href.replace(/^tel:/, '') : null;
  return {
    label: c.label || (testMode ? 'Call the SENTRIX Desk' : 'Call 112 now'),
    number,
    note: number ? (c.test_mode ? 'In this demo the call button reaches the SENTRIX Desk (simulated), never 112.' : null) : c.test_mode ? 'Calling is off in this demo.' : null,
  };
}

export function toDeviceCard(d: RawDevice, live?: { level?: number; incidentType?: string; unusual?: boolean }): DeviceCard {
  const openReq = d.requests?.find((r) => ['PENDING', 'DRAFTED', 'APPROVED', 'DISPATCHED', 'ACKNOWLEDGED', 'FAILED'].includes(r.state));
  let status: DeviceCard['status'] = 'clear';
  if (!d.claimed) status = 'unclaimed';
  else if (!d.online) status = 'offline';
  else if (openReq || (live?.level ?? 1) >= 3) status = 'alert';
  else if ((live?.level ?? 1) === 2) status = 'unusual';
  const li = d.last_incident;
  return {
    id: d.id,
    name: d.name,
    serial: d.serial,
    room: d.room,
    floor: null,
    online: d.online,
    claimed: d.claimed,
    lastSeen: d.last_seen,
    mode: d.armed ? 'away' : 'home',
    aiRate: d.ai_hz,
    level: live?.level ?? null,
    status,
    statusText: live?.incidentType ?? null,
    lastIncident: li ? { type: li.incident_type ?? li.type ?? 'incident', level: Number(li.peak_level ?? li.level ?? 0), at: li.started_at ?? li.at ?? '', reviewed: Boolean(li.verdict) } : null,
  };
}

/* ------------------------------------------------------------ hooks */
export function useHome(enabled = true) {
  return useQuery({ queryKey: ['home'], queryFn: () => api.get<HomeOut>('/api/v2/home'), refetchInterval: 15_000, enabled });
}

/** Open requests for the global alert bar (polled; the Portal also pushes them). */
export function useOpenRequests(enabled = true) {
  return useQuery({
    queryKey: ['sos-open'],
    queryFn: () => api.get<{ requests: RawRequest[] }>('/api/v2/sos/open'),
    refetchInterval: 5_000,
    enabled,
  });
}
