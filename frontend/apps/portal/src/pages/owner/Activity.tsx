import { ApiError } from '@sentrix/api';
import { Button, EmptyState, ErrorState, List, ListRow, PinPad, Segmented, Sheet, Spinner, Strip, dayKey, dayLabel, formatDayTime, formatDuration, useToast } from '@sentrix/ui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { ActivityRow, activityState, needsLook } from '../../features/activity/ActivityRow';
import { deviceApi, useSession } from '../../lib/api';
import { toActivityItem, type DeviceIncident } from '../../lib/activity';
import { useHome, type RawDevice, type RawRequest } from '../../lib/data';
import type { ActivityItem } from '../../lib/models';
import { api } from '../../lib/api';

interface MirrorOut {
  online: boolean;
  incidents: { id: string; started_at: string | null; ended_at: string | null; incident_type: string | null; peak_level: number | null; reason: string | null; verdict: string | null }[];
}

/** A device's incidents: from the device through the tunnel while it is
 *  online, else what the Portal keeps of it (no snapshots, no review). */
function useDeviceItems(d: RawDevice) {
  const live = useQuery({
    queryKey: ['device-activity', d.id],
    queryFn: () => deviceApi(d.id).get<{ items: DeviceIncident[] }>('/api/activity?limit=100'),
    enabled: d.claimed && d.online,
    refetchInterval: 20_000,
    retry: false,
  });
  const mirror = useQuery({
    queryKey: ['device-mirror', d.id],
    queryFn: () => api.get<MirrorOut>(`/api/v2/devices/${d.id}/mirror`),
    enabled: d.claimed && (!d.online || live.isError),
  });
  const fromDevice = Boolean(live.data);
  const items: ActivityItem[] = live.data
    ? live.data.items.map((i) => toActivityItem(i, d.id))
    : (mirror.data?.incidents ?? []).map((i) =>
        toActivityItem(
          { id: i.id, started_at: i.started_at, ended_at: i.ended_at, ongoing: !i.ended_at, duration_seconds: i.started_at && i.ended_at ? (Date.parse(i.ended_at) - Date.parse(i.started_at)) / 1000 : null, peak_level: i.peak_level, incident_type: i.incident_type, reason: i.reason, room: d.room, verdict: i.verdict, verdict_note: null, request: null, snapshot_url: null },
          d.id,
        ),
      );
  return { items, fromDevice, loading: live.isLoading && mirror.isLoading };
}

type Filter = 'all' | 'alerts' | 'look';

export function Activity() {
  const { data: s } = useSession();
  const home = useHome();
  const [params, setParams] = useSearchParams();
  const [filter, setFilter] = useState<Filter>('all');
  const tz = s?.timezone ?? 'Asia/Kolkata';
  if (home.isLoading) return <Spinner />;
  if (!home.data) return <div className="p-4"><ErrorState message="Could not load activity." onRetry={() => home.refetch()} /></div>;
  const devices = home.data.devices.filter((d) => d.claimed);
  return (
    <div className="grid gap-4 px-4 py-4 safe-x md:px-6">
      <h1 className="text-[22px] font-bold">Activity</h1>
      <Segmented
        label="Show"
        value={filter}
        onChange={setFilter}
        options={[
          { value: 'all', label: 'Everything' },
          { value: 'alerts', label: 'Alerts' },
          { value: 'look', label: 'To look at' },
        ]}
      />
      {devices.length === 0 ? (
        <EmptyState title="Nothing yet">Activity appears here once your device is added.</EmptyState>
      ) : (
        devices.map((d) => <DeviceActivity key={d.id} device={d} requests={home.data!.requests} filter={filter} tz={tz} openId={params.get('item')} onOpen={(id) => setParams(id ? { item: id } : {}, { replace: true })} />)
      )}
      <RequestHistory tz={tz} />
    </div>
  );
}

function DeviceActivity({ device, filter, tz, openId, onOpen }: { device: RawDevice; requests: RawRequest[]; filter: Filter; tz: string; openId: string | null; onOpen: (id: string | null) => void }) {
  const { items, fromDevice, loading } = useDeviceItems(device);
  const shown = useMemo(() => items.filter((a) => (filter === 'alerts' ? a.kind === 'alert' : filter === 'look' ? needsLook(a) : true)), [items, filter]);
  const groups = useMemo(() => {
    const g = new Map<string, ActivityItem[]>();
    for (const a of shown) {
      const k = dayKey(a.at, tz);
      g.set(k, [...(g.get(k) ?? []), a]);
    }
    return [...g.entries()];
  }, [shown, tz]);
  const open = items.find((a) => a.id === openId) ?? null;
  if (loading) return <Spinner />;
  return (
    <section aria-label={device.name} className="grid gap-3">
      {!fromDevice ? <Strip tone="warn">{device.name} is offline. This is what the SENTRIX server kept; pictures and reviews come back when it reconnects.</Strip> : null}
      {groups.length === 0 ? <p className="text-[13.5px] text-slate">{filter === 'look' ? 'Nothing to look at.' : 'Nothing here yet.'}</p> : null}
      {groups.map(([day, list]) => (
        <div key={day} className="grid gap-1.5">
          <h2 className="text-[13px] font-bold text-slate">{dayLabel(list[0].at, tz)}</h2>
          {list.map((a) => (
            <ActivityRow key={a.id} item={a} timezone={tz} onOpen={() => onOpen(a.id)} />
          ))}
        </div>
      ))}
      <Sheet open={Boolean(open)} onClose={() => onOpen(null)} title={open?.title}>
        {open ? <ActivityDetail item={open} reviewable={fromDevice} tz={tz} onDone={() => onOpen(null)} /> : null}
      </Sheet>
    </section>
  );
}

const REVIEW = [
  { key: 'guest', label: 'A guest or someone I know', verdict: 'FALSE' },
  { key: 'resident', label: 'Someone who lives here', verdict: 'FALSE' },
  { key: 'unknown', label: 'I don’t know them', verdict: 'TRUE' },
] as const;

function ActivityDetail({ item: a, reviewable, tz, onDone }: { item: ActivityItem; reviewable: boolean; tz: string; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [pin, setPin] = useState(false);
  const [pinError, setPinError] = useState<string>();

  async function verdict(v: 'TRUE' | 'FALSE', note?: string, p?: string) {
    if (!a.deviceId || !a.incidentId) return;
    setBusy(true);
    const form = new FormData();
    form.set('verdict', v);
    if (note) form.set('note', note);
    if (p) form.set('pin', p);
    try {
      await deviceApi(a.deviceId).post(`/api/incidents/${encodeURIComponent(a.incidentId)}/verdict`, form);
      toast(note?.startsWith('review:') ? 'Thanks. Saved.' : v === 'FALSE' ? 'Marked as false alarm.' : 'Marked as real.');
      setPin(false);
      await qc.invalidateQueries({ queryKey: ['device-activity', a.deviceId] });
      onDone();
    } catch (err) {
      const e = err as ApiError;
      if (e?.code === 'pin_required' || e?.code === 'pin_invalid' || e?.code === 'pin_locked') {
        setPin(true);
        setPinError(e.code === 'pin_required' ? undefined : e.message);
      } else toast(err instanceof ApiError ? err.message : 'That did not save.');
    } finally {
      setBusy(false);
    }
  }

  if (pin) return <PinPad title="Enter your SOS PIN to mark this as a false alarm" error={pinError} busy={busy} onCancel={() => setPin(false)} onSubmit={(p) => verdict('FALSE', undefined, p)} />;
  return (
    <div className="grid gap-3">
      {a.snapshotUrl ? <img src={a.snapshotUrl} alt={`Picture taken during “${a.title}”`} className="w-full rounded-xl bg-hatch" /> : null}
      <List>
        <ListRow label="When" value={formatDayTime(a.at, tz)} />
        {a.where ? <ListRow label="Where" value={a.where} /> : null}
        <ListRow label="How long" value={a.live ? 'Still going' : a.durationS ? formatDuration(a.durationS) : 'A moment'} />
        <ListRow label="Status" value={activityState(a)} />
        {a.outcome ? <ListRow label="Help request" value={a.outcome} /> : null}
      </List>
      {a.kind === 'unusual' ? <p className="text-[12.5px] text-slate">Only you could see this. Nobody else was told.</p> : null}
      {a.requestId ? (
        <Button onClick={() => navigate(`/alert/${a.requestId}`)}>Open the alert</Button>
      ) : null}
      {reviewable && !a.live && a.kind === 'unusual' ? (
        <div className="grid gap-2">
          <p className="text-[14px] font-semibold">Who was it?</p>
          {REVIEW.map((r) => (
            <Button key={r.key} busy={busy} variant={a.review === r.key ? 'primary' : 'secondary'} onClick={() => verdict(r.verdict, `review:${r.key}`)}>
              {r.label}
            </Button>
          ))}
          <p className="text-[12px] text-slate">Your answer helps SENTRIX learn your home. It changes no alert.</p>
        </div>
      ) : null}
      {reviewable && !a.live && a.kind === 'alert' ? (
        <div className="grid gap-2">
          <p className="text-[14px] font-semibold">Was this right?</p>
          <div className="grid grid-cols-2 gap-2">
            <Button busy={busy} variant={a.verdict === 'true' ? 'primary' : 'secondary'} onClick={() => verdict('TRUE')}>
              Yes, it was real
            </Button>
            <Button busy={busy} variant={a.verdict === 'false' ? 'primary' : 'secondary'} onClick={() => (a.requestId ? setPin(true) : verdict('FALSE'))}>
              False alarm
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** Help requests the SENTRIX server keeps (they outlive the device's own log). */
function RequestHistory({ tz }: { tz: string }) {
  const navigate = useNavigate();
  const q = useQuery({ queryKey: ['sos-list'], queryFn: () => api.get<{ open: RawRequest[]; recent: RawRequest[] }>('/api/v2/sos?limit=30') });
  const rows = [...(q.data?.open ?? []), ...(q.data?.recent ?? [])];
  if (!rows.length) return null;
  return (
    <section className="grid gap-2" aria-labelledby="req-h">
      <h2 id="req-h" className="text-[16px] font-bold">Help requests</h2>
      <List>
        {rows.map((r) => (
          <ListRow key={r.id} label={r.label} sub={`${formatDayTime(r.created_at ?? '', tz)}. Request ${r.short_id}`} value={r.state === 'PENDING' ? 'Needs your answer' : 'Open'} tone={r.state === 'PENDING' ? 'bad' : undefined} onClick={() => navigate(`/alert/${r.id}`)} />
        ))}
      </List>
    </section>
  );
}
