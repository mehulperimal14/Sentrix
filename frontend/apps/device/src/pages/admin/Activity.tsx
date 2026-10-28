import { ApiError } from '@sentrix/api';
import { Button, EmptyState, PinPad, Pill, Select, Sheet, Spinner, Strip, formatDayTime, formatDuration, useToast } from '@sentrix/ui';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api, useSession } from '../../lib/api';
import { KV, Panel } from './ui';

const TYPES = ['', 'weapon', 'fire', 'intrusion', 'assault', 'robbery', 'duress', 'normal'];
type Tab = 'incidents' | 'snapshots' | 'evidence' | 'dispatch';

/** Incidents with verdicts, snapshots, sealed evidence and dispatch packages. */
export default function Activity() {
  const [tab, setTab] = useState<Tab>('incidents');
  const tabs: [Tab, string][] = [
    ['incidents', 'Incidents'],
    ['snapshots', 'Snapshots'],
    ['evidence', 'Evidence'],
    ['dispatch', 'Dispatch'],
  ];
  return (
    <div className="grid gap-4">
      <h1 className="text-[24px] font-bold">Activity</h1>
      <div role="tablist" aria-label="Activity" className="flex flex-wrap gap-1.5">
        {tabs.map(([k, l]) => (
          <button key={k} role="tab" aria-selected={tab === k} type="button" onClick={() => setTab(k)} className={tab === k ? 'rounded-full bg-graphite px-3 py-1 text-[13px] font-semibold text-paper' : 'rounded-full border border-line bg-paper px-3 py-1 text-[13px] font-semibold text-slate'}>
            {l}
          </button>
        ))}
      </div>
      {tab === 'incidents' ? <Incidents /> : tab === 'snapshots' ? <Snapshots /> : tab === 'evidence' ? <Evidence /> : <Dispatch />}
    </div>
  );
}

function Incidents() {
  const session = useSession();
  const tz = session.data?.timezone ?? 'Asia/Kolkata';
  const qc = useQueryClient();
  const toast = useToast();
  const [type, setType] = useState('');
  const [verdict, setVerdict] = useState('');
  const [pinFor, setPinFor] = useState<string | null>(null);
  const [pinError, setPinError] = useState<string>();
  const q = useInfiniteQuery({
    queryKey: ['activity', type, verdict],
    initialPageParam: '',
    queryFn: ({ pageParam }) => {
      const p = new URLSearchParams({ limit: '50' });
      if (pageParam) p.set('cursor', pageParam);
      if (type) p.set('incident_type', type);
      if (verdict) p.set('verdict', verdict);
      return api.get<any>(`/api/activity?${p}`);
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const set = useMutation({
    mutationFn: ({ id, v, pin }: { id: string; v: 'TRUE' | 'FALSE'; pin?: string }) => {
      const form = new FormData();
      form.set('verdict', v);
      if (pin) form.set('pin', pin);
      return api.post(`/api/incidents/${encodeURIComponent(id)}/verdict`, form);
    },
    onSuccess: () => {
      setPinFor(null);
      setPinError(undefined);
      toast('Saved.');
      qc.invalidateQueries({ queryKey: ['activity'] });
    },
    onError: (e, vars) => {
      const err = e instanceof ApiError ? e : null;
      if (err?.code === 'pin_invalid' && !vars.pin) {
        setPinFor(vars.id);
        return;
      }
      if (vars.pin) setPinError(err?.message ?? 'That PIN is not right.');
      else toast(err?.message ?? 'Could not save the verdict.');
    },
  });
  if (q.isLoading) return <Spinner />;
  if (q.isError) return <Strip tone="warn">Could not load the incidents.</Strip>;
  const pages = q.data?.pages ?? [];
  const items = pages.flatMap((p) => p.items as any[]);
  const stats = pages[0]?.stats;
  return (
    <div className="grid gap-3">
      {stats ? (
        <div className="flex flex-wrap gap-2 text-[13px]">
          <Pill tone="info">{stats.total} incidents</Pill>
          <Pill tone="alarm">{stats.confirmed} real</Pill>
          <Pill tone="ok">{stats.false_alarms} false alarms</Pill>
          <Pill tone="warn">{stats.unreviewed} not looked at</Pill>
        </div>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <label className="grid gap-1 text-[12.5px] font-semibold">
          Type
          <Select value={type} onChange={(e) => setType(e.target.value)} className="min-h-9 w-44 py-1">
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {t || 'Any'}
              </option>
            ))}
          </Select>
        </label>
        <label className="grid gap-1 text-[12.5px] font-semibold">
          Verdict
          <Select value={verdict} onChange={(e) => setVerdict(e.target.value)} className="min-h-9 w-44 py-1">
            <option value="">Any</option>
            <option value="TRUE">Real</option>
            <option value="FALSE">False alarm</option>
            <option value="unreviewed">Not looked at</option>
          </Select>
        </label>
      </div>
      {items.length === 0 ? (
        <EmptyState title="No incidents">An incident opens at Level 2 and above.</EmptyState>
      ) : (
        <div className="overflow-x-auto rounded-2xl border border-line bg-paper">
          <table className="w-full min-w-[860px] text-left text-[13px]">
            <thead className="text-[12px] text-slate">
              <tr>
                {['Started', 'Level', 'Type', 'Why', 'Length', 'Request', 'Picture', 'Verdict'].map((h) => (
                  <th key={h} className="px-3 py-2 font-semibold">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((it) => (
                <tr key={it.id} className="border-t border-line align-top">
                  <td className="px-3 py-2 whitespace-nowrap">{it.started_at ? formatDayTime(it.started_at, tz) : '—'}</td>
                  <td className="px-3 py-2">
                    <Pill tone={it.peak_level >= 3 ? 'alarm' : it.peak_level === 2 ? 'warn' : 'ok'}>L{it.peak_level}</Pill>
                  </td>
                  <td className="px-3 py-2">{it.incident_type}</td>
                  <td className="max-w-[260px] px-3 py-2 text-slate" title={it.reason ?? ''}>
                    <span className="line-clamp-2">{it.reason}</span>
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap">{it.ongoing ? 'ongoing' : it.duration_seconds != null ? formatDuration(it.duration_seconds) : '—'}</td>
                  <td className="px-3 py-2">{it.request ? `${it.request.label ?? it.request.state} (${it.request.short_id})` : '—'}</td>
                  <td className="px-3 py-2">
                    {it.snapshot_url ? (
                      <a className="font-semibold text-indigo underline" href={it.snapshot_url} target="_blank" rel="noopener">
                        Picture
                      </a>
                    ) : (
                      '—'
                    )}
                    {it.evidence_url ? (
                      <a className="ml-2 font-semibold text-indigo underline" href={it.evidence_url} target="_blank" rel="noopener">
                        Evidence
                      </a>
                    ) : null}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-1.5">
                      <Button size="sm" block={false} variant={it.verdict === 'TRUE' ? 'dark' : 'secondary'} aria-pressed={it.verdict === 'TRUE'} onClick={() => set.mutate({ id: it.id, v: 'TRUE' })}>
                        Real
                      </Button>
                      <Button size="sm" block={false} variant={it.verdict === 'FALSE' ? 'dark' : 'secondary'} aria-pressed={it.verdict === 'FALSE'} onClick={() => set.mutate({ id: it.id, v: 'FALSE' })}>
                        False alarm
                      </Button>
                    </div>
                    {it.verdict_by ? <small className="mt-1 block text-[11.5px] text-slate">{it.verdict_by}</small> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {q.hasNextPage ? (
        <Button block={false} busy={q.isFetchingNextPage} onClick={() => q.fetchNextPage()}>
          Show older
        </Button>
      ) : null}
      <Sheet open={pinFor !== null} onClose={() => setPinFor(null)}>
        <PinPad
          title="This incident has an open help request. Enter the SOS PIN to mark it as a false alarm."
          error={pinError}
          busy={set.isPending}
          onCancel={() => setPinFor(null)}
          onSubmit={(pin) => set.mutate({ id: pinFor!, v: 'FALSE', pin })}
        />
      </Sheet>
    </div>
  );
}

function Snapshots() {
  const tz = useSession().data?.timezone ?? 'Asia/Kolkata';
  const [open, setOpen] = useState<string | null>(null);
  const q = useQuery({ queryKey: ['snapshots'], queryFn: () => api.get<any>('/api/snapshots?limit=120') });
  if (q.isLoading) return <Spinner />;
  const items: any[] = q.data?.items ?? [];
  if (!items.length) return <EmptyState title="No snapshots yet">A picture is kept from Level 3 and above.</EmptyState>;
  return (
    <>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        {items.map((s) => (
          <button key={s.name} type="button" onClick={() => setOpen(s.url)} className="grid gap-1 rounded-xl border border-line bg-paper p-2 text-left text-[12px]">
            <img src={s.url} alt={`Snapshot ${s.taken_at ? formatDayTime(s.taken_at, tz) : s.name}`} loading="lazy" className="aspect-video w-full rounded-lg bg-hatch object-cover" />
            <span className="font-semibold">{s.incident?.incident_type ?? 'snapshot'}</span>
            <span className="text-slate">{s.taken_at ? formatDayTime(s.taken_at, tz) : s.name}</span>
          </button>
        ))}
      </div>
      <Sheet open={open !== null} onClose={() => setOpen(null)} title="Snapshot">
        {open ? <img src={open} alt="Snapshot" className="w-full rounded-xl" /> : null}
      </Sheet>
    </>
  );
}

function Evidence() {
  const tz = useSession().data?.timezone ?? 'Asia/Kolkata';
  const q = useQuery({ queryKey: ['evidence'], queryFn: () => api.get<any>('/api/evidence?limit=200') });
  if (q.isLoading) return <Spinner />;
  const d = q.data ?? {};
  const chain = d.chain ?? {};
  const items: any[] = d.items ?? [];
  return (
    <div className="grid gap-3">
      <Strip tone={chain.ok ? 'ok' : 'alarm'}>
        {chain.ok ? `Chain verified: ${chain.entries} entries, no problems.` : `Chain check: ${chain.problem_count ?? 0} problems${chain.error ? `. ${chain.error}` : ''}.`} Evidence is encrypted (AES-256-GCM) and tamper-evident, not court-admissible.
      </Strip>
      {items.length === 0 ? (
        <EmptyState title="No evidence yet">Encrypted evidence is saved from Level 3 and above.</EmptyState>
      ) : (
        <div className="overflow-x-auto rounded-2xl border border-line bg-paper">
          <table className="w-full min-w-[760px] text-left text-[13px]">
            <thead className="text-[12px] text-slate">
              <tr>
                {['Time', 'Level', 'Type', 'Room', 'Detections', 'SHA-256', ''].map((h) => (
                  <th key={h} className="px-3 py-2 font-semibold">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((e) => (
                <tr key={e.bundle_id} className="border-t border-line">
                  <td className="px-3 py-2 whitespace-nowrap">{e.timestamp ? formatDayTime(e.timestamp, tz) : '—'}</td>
                  <td className="px-3 py-2">L{e.level}</td>
                  <td className="px-3 py-2">{e.incident_type}</td>
                  <td className="px-3 py-2">{e.room ?? '—'}</td>
                  <td className="px-3 py-2 text-slate">{(e.detections ?? []).slice(0, 3).map((x: any) => `${x.label}${x.conf != null ? ` ${x.conf.toFixed(2)}` : ''}`).join(', ') || '—'}</td>
                  <td className="px-3 py-2 font-mono text-[12px]" title={e.sha256}>
                    {e.sha256?.slice(0, 16)}…
                  </td>
                  <td className="px-3 py-2">
                    <a className="font-semibold text-indigo underline" href={e.view_url} target="_blank" rel="noopener">
                      View
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Dispatch() {
  const tz = useSession().data?.timezone ?? 'Asia/Kolkata';
  const q = useQuery({ queryKey: ['dispatch'], queryFn: () => api.get<any>('/api/dispatch') });
  if (q.isLoading) return <Spinner />;
  const items: any[] = q.data?.items ?? [];
  if (!items.length) return <EmptyState title="No dispatch packages">A package is prepared with each help request.</EmptyState>;
  return (
    <div className="grid gap-3 md:grid-cols-2">
      {items.map((p) => (
        <Panel key={p.id} title={`${p.incident_type ?? 'Incident'}, level ${p.level ?? '—'}`}>
          <KV
            rows={[
              ['Prepared', p.created_at ? formatDayTime(p.created_at, tz) : '—'],
              ['Status', p.status ?? '—'],
              ['Recommended', p.authority ?? '—'],
              ['Chosen', p.chosen_authority ?? '—'],
              ['Sent to', p.sent_to ?? '—'],
              ['Request', p.request ? `${p.request.label ?? ''} (${p.request.short_id})` : '—'],
            ]}
          />
        </Panel>
      ))}
    </div>
  );
}
