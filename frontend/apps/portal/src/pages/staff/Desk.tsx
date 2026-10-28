import { ApiError } from '@sentrix/api';
import { Band, Button, EmptyState, ErrorState, Field, List, ListRow, Pill, Select, Spinner, Strip, TextInput, formatDayTime, formatTime, useToast } from '@sentrix/ui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { MapPin, Phone, Volume2, VolumeX } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import { headline } from '../../features/alert/AlertScreen';
import { api, useSession } from '../../lib/api';
import { historyOf, type RawEvent, type RawRequest } from '../../lib/data';

type DeskCard = RawRequest & { duress: boolean; banner: string | null; city: string | null; locality: string | null; resident: string | null; acknowledged: boolean };

const STATE: Record<string, { text: string; tone: 'alarm' | 'warn' | 'ok' | 'info' | 'off' }> = {
  APPROVED: { text: 'New', tone: 'alarm' },
  DISPATCHED: { text: 'New', tone: 'alarm' },
  ACKNOWLEDGED: { text: 'Acknowledged', tone: 'ok' },
  FAILED: { text: 'Not delivered', tone: 'warn' },
  CLOSED: { text: 'Closed', tone: 'off' },
  PENDING: { text: 'Waiting for the resident', tone: 'info' },
};

/** A short repeating tone while a request waits for an operator. The browser
 *  allows sound only after a click, so the operator turns it on. */
function useAlarmTone(active: boolean, on: boolean) {
  const ctx = useRef<AudioContext | null>(null);
  useEffect(() => {
    if (!active || !on) return;
    ctx.current ??= new AudioContext();
    const ac = ctx.current;
    const beep = () => {
      const o = ac.createOscillator();
      const g = ac.createGain();
      o.frequency.value = 880;
      g.gain.setValueAtTime(0.0001, ac.currentTime);
      g.gain.exponentialRampToValueAtTime(0.2, ac.currentTime + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ac.currentTime + 0.35);
      o.connect(g).connect(ac.destination);
      o.start();
      o.stop(ac.currentTime + 0.4);
    };
    beep();
    const t = setInterval(beep, 2500);
    return () => clearInterval(t);
  }, [active, on]);
}

export function DeskQueue() {
  const navigate = useNavigate();
  const { data: s } = useSession();
  const [sound, setSound] = useState(false);
  const q = useQuery({ queryKey: ['desk-queue'], queryFn: () => api.get<{ requests: DeskCard[]; unacknowledged: number }>('/api/v2/desk/queue'), refetchInterval: 3000 });
  useAlarmTone((q.data?.unacknowledged ?? 0) > 0, sound);
  if (q.isLoading) return <Spinner />;
  if (!q.data) return <ErrorState message="Could not load the queue." onRetry={() => q.refetch()} />;
  const tz = s?.timezone ?? 'Asia/Kolkata';
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-[22px] font-bold">Requests</h1>
        <div className="flex items-center gap-3">
          {q.data.unacknowledged > 0 ? <Pill tone="alarm">{q.data.unacknowledged} waiting for an operator</Pill> : <Pill tone="ok">Nothing waiting</Pill>}
          <Button size="sm" block={false} icon={sound ? <Volume2 aria-hidden className="size-4" /> : <VolumeX aria-hidden className="size-4" />} onClick={() => setSound(!sound)}>
            {sound ? 'Sound on' : 'Turn sound on'}
          </Button>
        </div>
      </div>
      {q.data.requests.length === 0 ? (
        <EmptyState title="No requests">New help requests appear here at once.</EmptyState>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-line bg-paper">
          <table className="w-full min-w-[640px] text-left text-[13.5px]">
            <thead className="text-[12px] text-slate">
              <tr>
                <th className="px-3 py-2 font-semibold">Received</th>
                <th className="px-3 py-2 font-semibold">What</th>
                <th className="px-3 py-2 font-semibold">Where</th>
                <th className="px-3 py-2 font-semibold">Resident</th>
                <th className="px-3 py-2 font-semibold">Status</th>
              </tr>
            </thead>
            <tbody>
              {q.data.requests.map((r) => {
                const st = STATE[r.state] ?? { text: r.state, tone: 'info' as const };
                return (
                  <tr key={r.id} onClick={() => navigate(`/desk/${r.id}`)} className="cursor-pointer border-t border-line hover:bg-chalk">
                    <td className="px-3 py-2.5 tabular">{r.created_at ? formatDayTime(r.created_at, tz) : ''}</td>
                    <td className="px-3 py-2.5 font-semibold">
                      <Link to={`/desk/${r.id}`} className="hover:underline">
                        {r.duress ? 'Emergency (duress)' : r.label}
                      </Link>
                      <small className="block font-normal text-slate">Request {r.short_id}</small>
                    </td>
                    <td className="px-3 py-2.5">{[r.locality, r.city].filter(Boolean).join(', ') || 'Unknown'}</td>
                    <td className="px-3 py-2.5">{r.resident ?? ''}</td>
                    <td className="px-3 py-2.5">
                      <Pill tone={r.duress && st.tone === 'alarm' ? 'alarm' : st.tone}>{st.text}</Pill>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

interface DeskRequestOut {
  request: DeskCard;
  events: (RawEvent & { restricted: boolean })[];
  address: string | null;
  entry_note: string | null;
  map_url: string | null;
  home: { adults: number | null; children: number | null; seniors: number | null; pets: string | null };
  medical: string | null;
  resident: string | null;
  resident_phone: string | null;
  room: string | null;
  has_snapshot: boolean;
  snapshot_url: string | null;
  live_url: string | null;
  outcomes: { value: string; label: string }[];
  call: { href: string | null; label: string; test_mode: boolean };
}

export function DeskRequest() {
  const { id } = useParams();
  const qc = useQueryClient();
  const toast = useToast();
  const { data: s } = useSession();
  const q = useQuery({ queryKey: ['desk', id], queryFn: () => api.get<DeskRequestOut>(`/api/v2/desk/requests/${encodeURIComponent(id!)}`), refetchInterval: 4000 });
  const [outcome, setOutcome] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string>();
  if (q.isLoading) return <Spinner />;
  if (!q.data) return <ErrorState title="Request not found" message="It is not in the Desk’s queue." />;
  const d = q.data;
  const r = d.request;
  const tz = s?.timezone ?? 'Asia/Kolkata';
  const closed = r.state === 'CLOSED';

  async function act(path: 'acknowledge' | 'called-112' | 'resolve', body: Record<string, string | null> = {}) {
    setBusy(path);
    setError(undefined);
    try {
      const out = await api.post<{ message: string }>(`/api/v2/desk/requests/${encodeURIComponent(id!)}/${path}`, body);
      toast(out.message);
      await Promise.all([qc.invalidateQueries({ queryKey: ['desk', id] }), qc.invalidateQueries({ queryKey: ['desk-queue'] })]);
    } catch (err) {
      setError(err instanceof ApiError ? (Object.values(err.fields)[0] ?? err.message) : 'That did not work.');
    } finally {
      setBusy(null);
    }
  }
  const home = [d.home.adults ? `${d.home.adults} adults` : null, d.home.seniors ? `${d.home.seniors} seniors` : null, d.home.children ? `${d.home.children} children` : null, d.home.pets].filter(Boolean).join(', ');
  return (
    <div className="grid gap-4">
      <Link to="/desk" className="justify-self-start text-[13px] font-semibold text-slate hover:text-graphite">
        All requests
      </Link>
      <Band tone={closed ? 'quiet' : r.duress || !r.acknowledged ? 'alarm' : 'help'} title={r.duress ? 'Emergency: the resident used the duress PIN' : headline({ incidentType: r.incident_type ?? 'threat', evidence: r.evidence_state, room: d.room })} right={<span className="text-[12px] tabular">Request {r.short_id}</span>}>
        {r.label}
      </Band>
      {r.banner ? <Strip tone="alarm">{r.banner}</Strip> : null}
      <div className="grid gap-4 lg:grid-cols-[1.2fr_1fr]">
        <div className="grid content-start gap-4">
          {d.snapshot_url ? <img src={d.snapshot_url} alt="Picture from the camera at the time of the alert" className="w-full rounded-xl bg-hatch" /> : <Strip tone="info">No picture: the household did not allow one, or none was taken.</Strip>}
          {d.live_url ? (
            <a href={d.live_url} target="_blank" rel="noopener" className="inline-flex justify-center rounded-xl border-[1.5px] border-line bg-paper px-4 py-2.5 font-semibold">
              Open the live picture
            </a>
          ) : null}
          <section className="grid gap-2">
            <h2 className="text-[15px] font-bold">Timeline</h2>
            <List>
              {historyOf(d.events).map((e, i) => (
                <ListRow key={i} label={e.text} value={formatTime(e.at, tz)} />
              ))}
            </List>
          </section>
        </div>
        <div className="grid content-start gap-4">
          <section className="grid gap-2 rounded-2xl bg-paper p-4">
            <h2 className="text-[15px] font-bold">Where</h2>
            <p className="text-[14px]">{d.address ?? 'No address on file.'}</p>
            {d.room ? <p className="text-[13px] text-slate">Room: {d.room}</p> : null}
            {d.entry_note ? <p className="text-[13px]">Entry: {d.entry_note}</p> : null}
            {d.map_url ? (
              <a href={d.map_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1.5 text-[13px] font-semibold text-indigo underline">
                <MapPin aria-hidden className="size-4" /> Open in maps
              </a>
            ) : null}
          </section>
          <section className="grid gap-2 rounded-2xl bg-paper p-4">
            <h2 className="text-[15px] font-bold">Who</h2>
            <p className="text-[14px]">{d.resident ?? 'Resident'}</p>
            {home ? <p className="text-[13px] text-slate">Lives here: {home}</p> : null}
            {d.medical ? <p className="text-[13px]">Medical: {d.medical}</p> : null}
            {d.resident_phone ? (
              <a href={`tel:${d.resident_phone}`} className="inline-flex items-center gap-1.5 text-[13px] font-semibold text-indigo underline">
                <Phone aria-hidden className="size-4" /> {d.resident_phone}
              </a>
            ) : r.duress ? (
              <Strip tone="alarm">Do not call the resident first.</Strip>
            ) : null}
          </section>
          {error ? <Strip tone="alarm" role="alert">{error}</Strip> : null}
          {!closed ? (
            <section className="grid gap-2 rounded-2xl bg-paper p-4">
              {!r.acknowledged ? (
                <Button variant="alarm" busy={busy === 'acknowledge'} onClick={() => act('acknowledge')}>
                  Acknowledge
                </Button>
              ) : null}
              <Button busy={busy === 'called-112'} onClick={() => act('called-112')} sub="A note in the timeline. SENTRIX does not contact 112 itself.">
                I called 112
              </Button>
              <Field label="Outcome" htmlFor="outcome">
                <Select id="outcome" value={outcome} onChange={(e) => setOutcome(e.target.value)}>
                  <option value="">Choose…</option>
                  {d.outcomes.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Note" htmlFor="note" optional>
                <TextInput id="note" value={note} onChange={(e) => setNote(e.target.value)} />
              </Field>
              <Button variant="primary" disabled={!outcome} busy={busy === 'resolve'} onClick={() => act('resolve', { outcome, note: note || null })}>
                Close the request
              </Button>
            </section>
          ) : (
            <Strip tone="ok">Closed{r.outcome ? `: ${r.outcome}` : ''}.</Strip>
          )}
        </div>
      </div>
    </div>
  );
}
