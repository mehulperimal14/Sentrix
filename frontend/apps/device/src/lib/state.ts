/** The device's live state as /ws/threat sends it (sentrix/pipeline/state.py).
 *  Only the keys the screens read are typed; the rest stay available as `any`. */

export interface SosBlock {
  active: boolean;
  request_id: string | null;
  short_id: string | null;
  state: string | null;
  label: string | null;
  incident_id: string | null;
  incident_type: string | null;
  level: number | null;
  urgency: string | null;
  evidence_state: string | null;
  created_at: string | null;
  expires_at: string | null;
  remaining_s: number | null;
  authority: string | null;
  recommended_authority: string | null;
  sos_text: string | null;
  snapshot: string | null;
  actions: string[];
  last: Record<string, any> | null;
}

export interface LiveState {
  tci: number;
  level: number;
  status: string;
  reason: string;
  incident_type: string;
  person_count: number;
  authorized: boolean;
  evidence_state: string;
  alarm: { level: number; status: string; since_s: number; incident_type: string; headline: string; deescalate_in: number | null };
  video_fps: number;
  ai_hz: number;
  frame_age_ms: number | null;
  camera_online: boolean;
  sos: SosBlock;
  link: { enabled: boolean; connected: boolean; claimed: boolean; state: string };
  mode: { armed: boolean; changed_by: string | null; changed_at: string | null };
  remote_viewers: number;
  recording?: { active?: boolean; started_at?: string; elapsed_s?: number } | null;
  [key: string]: any;
}

const NOUN: Record<string, string> = { weapon: 'weapon', fire: 'fire', assault: 'fight', intrusion: 'intruder' };

/** "Possible fire in the Living room" / "Fire confirmed in the Living room". */
export function alertHeadline(type: string | null | undefined, evidence: string | null | undefined, room?: string | null): string {
  const noun = NOUN[type ?? ''] ?? 'threat';
  const where = room ? ` in the ${room}` : '';
  if (evidence === 'corroborated') return `${noun.charAt(0).toUpperCase()}${noun.slice(1)} confirmed${where}`;
  return `Possible ${noun}${where}`;
}

/** The contract's evidence phrases (docs/7 §7.10). */
export function evidencePhrase(evidence: string | null | undefined): string {
  if (evidence === 'corroborated') return 'Confirmed by two different models';
  if (evidence === 'degraded') return 'Camera view degraded (dark), detection unreliable';
  return 'Possible (one camera check)';
}

export function defaultAuthority(type: string | null | undefined): 'POLICE' | 'FIRE' | 'MEDICAL' {
  return type === 'fire' ? 'FIRE' : 'POLICE';
}

/** Plain words for the level ladder of the Experience Plan. */
export function levelWords(state: LiveState): { title: string; tone: 'ok' | 'warn' | 'alarm' } {
  const level = state.alarm?.level ?? state.level ?? 1;
  const type = state.alarm?.incident_type ?? state.incident_type;
  if (level >= 3) return { title: alertHeadline(type, state.evidence_state), tone: 'alarm' };
  if (level === 2) return { title: type === 'intrusion' ? 'Someone we don’t recognise' : 'Something unusual', tone: 'warn' };
  return { title: 'All clear', tone: 'ok' };
}
