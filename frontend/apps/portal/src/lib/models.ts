/** View models the screens render. `lib/data.ts` maps API responses onto them,
 *  so screens never depend on the exact JSON field names. */

export type Authority = 'POLICE' | 'FIRE' | 'MEDICAL';
export type RequestState = 'DRAFTED' | 'PENDING' | 'APPROVED' | 'DISPATCHED' | 'ACKNOWLEDGED' | 'FAILED' | 'CLOSED' | 'DECLINED' | 'EXPIRED' | 'CANCELLED';
export type AnswerAction = 'approve' | 'checking' | 'decline' | 'safe' | 'help_now' | 'call_112';

export interface HelpRequest {
  id: string;
  shortId: string;
  state: RequestState;
  label: string;
  incidentType: string;
  level: number | null;
  evidence: string | null;
  createdAt: string;
  expiresAt: string | null;
  checkingUntil: string | null;
  room: string | null;
  snapshotUrl: string | null;
  actions: AnswerAction[];
  authority: Authority | null;
  recommendedAuthority: Authority | null;
  deviceId: string | null;
  linked: boolean;
  approvedAt: string | null;
  approvedBy: string | null;
  contacts: { name: string; status: string; tone?: 'ok' | 'warn' | 'bad' }[];
  desk: { status: string; tone?: 'ok' | 'warn' | 'bad' } | null;
  safetyNote: string | null;
  sosText: string | null;
  history: { at: string; text: string }[];
  outcome: string | null;
  residentSafe: boolean;
}

export interface CallInfo {
  label: string;
  number: string | null;
  note: string | null;
}

export interface DeviceCard {
  id: string;
  name: string;
  serial: string;
  room: string | null;
  floor: string | null;
  online: boolean;
  claimed: boolean;
  lastSeen: string | null;
  mode: 'home' | 'away' | null;
  aiRate: number | null;
  level: number | null;
  status: 'clear' | 'unusual' | 'alert' | 'offline' | 'unclaimed';
  statusText: string | null;
  lastIncident: { type: string; level: number; at: string; reviewed: boolean } | null;
}

export type ActivityKind = 'alert' | 'unusual' | 'system';

export interface ActivityItem {
  id: string;
  kind: ActivityKind;
  title: string;
  at: string;
  endedAt: string | null;
  where: string | null;
  durationS: number | null;
  live: boolean;
  verdict: 'true' | 'false' | null;
  review: string | null;
  evidence: string | null;
  outcome: string | null;
  told: string | null;
  snapshotUrl: string | null;
  requestId: string | null;
  incidentId: string | null;
  deviceId: string | null;
  note: string | null;
}

export interface Contact {
  id: string;
  name: string;
  relation: string;
  phone: string;
  email: string | null;
  status: 'waiting' | 'confirmed' | 'declined' | 'revoked';
  telegram: boolean;
  priority: number | null;
  shareLink: string | null;
}

export interface Member {
  id: string;
  name: string;
  relation: string | null;
  ageBand: 'adult' | 'senior' | 'minor';
  hasAccount: boolean;
  invited: boolean;
  faceEnrolled: boolean | null;
}
