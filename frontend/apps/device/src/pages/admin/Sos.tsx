import { ApiError } from '@sentrix/api';
import { Button, EmptyState, Select, Spinner, Strip, formatDayTime, useToast } from '@sentrix/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api, useSession } from '../../lib/api';
import { Panel } from './ui';

/** Help requests on this device: history, the SOS preview and the test-mode
 *  demo controls (the scripted trigger must be disclosed to the audience). */
export default function Sos() {
  const session = useSession();
  const s = session.data;
  const tz = s?.timezone ?? 'Asia/Kolkata';
  const qc = useQueryClient();
  const toast = useToast();
  const [kind, setKind] = useState('weapon');
  const raw = useQuery({ queryKey: ['session-raw'], queryFn: () => api.get<any>('/api/session') });
  const history = useQuery({ queryKey: ['sos-history'], queryFn: () => api.get<any>('/api/sos/requests?limit=50'), refetchInterval: 10_000 });
  const preview = useQuery({ queryKey: ['sos-preview'], queryFn: () => api.get<any>('/api/sos/preview?incident_type=weapon&level=3') });
  const demo = useMutation({
    mutationFn: (what: 'close' | 'trigger') => (what === 'close' ? api.post('/api/sos/demo/close-incident', {}) : api.post('/api/sos/demo/trigger', { incident_type: kind })),
    onSuccess: (_d, what) => {
      toast(what === 'close' ? 'The current incident closes after the next decision.' : 'Simulated detection opened. Tell the audience it is scripted.');
      qc.invalidateQueries({ queryKey: ['sos-history'] });
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'That did not work.'),
  });
  const feats = raw.data?.sos ?? {};
  const rows: any[] = history.data?.requests ?? [];
  return (
    <div className="grid gap-4">
      <h1 className="text-[24px] font-bold">Help requests</h1>
      {feats.enabled === false ? <Strip tone="warn">The SOS service is off on this unit: no request opens and nobody is prompted.</Strip> : null}
      <div className="grid gap-4 lg:grid-cols-[1.4fr_1fr]">
        <Panel title="History">
          {history.isLoading ? (
            <Spinner />
          ) : rows.length === 0 ? (
            <EmptyState title="No help requests yet" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] text-left text-[13px]">
                <thead className="text-[12px] text-slate">
                  <tr>
                    {['Opened', 'Reference', 'Type', 'Level', 'State', 'Answered'].map((h) => (
                      <th key={h} className="py-1.5 pr-3 font-semibold">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.request_id ?? r.id} className="border-t border-line">
                      <td className="py-1.5 pr-3 whitespace-nowrap">{r.created_at ? formatDayTime(r.created_at, tz) : '—'}</td>
                      <td className="py-1.5 pr-3 font-mono text-[12px]">{r.short_id}</td>
                      <td className="py-1.5 pr-3">{r.incident_type}</td>
                      <td className="py-1.5 pr-3">{r.level}</td>
                      <td className="py-1.5 pr-3">{r.label ?? r.state}</td>
                      <td className="py-1.5 pr-3 text-slate">{r.answered_at ? formatDayTime(r.answered_at, tz) : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
        <div className="grid content-start gap-4">
          <Panel title="What contacts and the Desk would get">
            {preview.data?.full || preview.data?.text ? (
              <pre className="whitespace-pre-wrap font-mono text-[11.5px] text-slate">{preview.data.full ?? preview.data.text}</pre>
            ) : (
              <p className="text-[13px] text-slate">No preview: the device has no household profile yet.</p>
            )}
          </Panel>
          {s?.testMode && (feats.demo_close_incident || feats.demo_trigger) ? (
            <Panel title="Demo controls (test mode only)">
              {feats.demo_close_incident ? (
                <Button busy={demo.isPending && demo.variables === 'close'} onClick={() => demo.mutate('close')}>
                  Close the current incident
                </Button>
              ) : null}
              {feats.demo_trigger ? (
                <div className="grid gap-2">
                  <Select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="What to simulate">
                    <option value="weapon">Weapon</option>
                    <option value="fire">Fire</option>
                    <option value="assault">Fight</option>
                  </Select>
                  <Button variant="alarm" busy={demo.isPending && demo.variables === 'trigger'} onClick={() => demo.mutate('trigger')} sub="Not a detection: disclose it to the audience">
                    Scripted trigger (SIMULATED DETECTION)
                  </Button>
                </div>
              ) : null}
            </Panel>
          ) : null}
        </div>
      </div>
    </div>
  );
}
