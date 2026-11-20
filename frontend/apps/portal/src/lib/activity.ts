import { dayKey } from '@sentrix/ui';
import { useQuery } from '@tanstack/react-query';
import { deviceApi, useSession } from './api';
import type { RawDevice } from './data';
import type { ActivityItem } from './models';

/** One incident from the device's GET /api/activity (sentrix/web/api_models.py). */
export interface DeviceIncident {
  id: string;
  started_at: string | null;
  ended_at: string | null;
  ongoing: boolean;
  duration_seconds: number | null;
  peak_level: number | null;
  incident_type: string | null;
  reason: string | null;
  room: string | null;
  verdict: string | null;
  verdict_note: string | null;
  request: { id: string; short_id: string; state: string; label: string } | null;
  snapshot_url: string | null;
}

const TITLES: Record<string, string> = {
  weapon: 'Possible weapon',
  fire: 'Possible fire',
  assault: 'A possible fight',
  robbery: 'Possible theft',
  intrusion: 'Someone we don’t recognise',
  duress: 'Emergency',
  normal: 'Something unusual',
};

export function toActivityItem(i: DeviceIncident, deviceId: string): ActivityItem {
  const level = i.peak_level ?? 0;
  const kind: ActivityItem['kind'] = level >= 3 ? 'alert' : 'unusual';
  const verdict = i.verdict === 'TRUE' ? 'true' : i.verdict === 'FALSE' ? 'false' : null;
  return {
    id: `${deviceId}:${i.id}`,
    kind,
    title: TITLES[i.incident_type ?? 'normal'] ?? 'Something unusual',
    at: i.started_at ?? new Date().toISOString(),
    endedAt: i.ended_at,
    where: i.room,
    durationS: i.duration_seconds,
    live: i.ongoing,
    verdict,
    review: kind === 'unusual' ? (i.verdict_note && /^review:/.test(i.verdict_note) ? i.verdict_note.slice(7) : verdict === 'false' ? 'guest' : verdict === 'true' ? 'unknown' : null) : null,
    evidence: null,
    outcome: i.request ? i.request.label : null,
    told: null,
    snapshotUrl: i.snapshot_url,
    requestId: i.request?.id ?? null,
    incidentId: i.id,
    deviceId,
    note: i.verdict_note,
  };
}

/** The device's incidents as Activity items (through the tunnel while online). */
export function useDeviceActivity(device: RawDevice, todayOnly?: number) {
  const { data: s } = useSession();
  const tz = s?.timezone ?? 'Asia/Kolkata';
  const q = useQuery({
    queryKey: ['device-activity', device.id],
    queryFn: () => deviceApi(device.id).get<{ items: DeviceIncident[] }>('/api/activity?limit=100'),
    enabled: device.claimed && device.online,
    refetchInterval: 20_000,
    retry: false,
  });
  let items = (q.data?.items ?? []).map((i) => toActivityItem(i, device.id));
  if (todayOnly) {
    const today = dayKey(new Date(), tz);
    items = items.filter((a) => dayKey(a.at, tz) === today).slice(0, todayOnly);
  }
  return { ...q, items };
}
