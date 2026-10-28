import { Pill, Spinner, Strip, formatDayTime } from '@sentrix/ui';
import { useQuery } from '@tanstack/react-query';
import { api, useLiveState, useSession } from '../../lib/api';
import { Bar, KV, Panel } from './ui';

const LEVEL_NAMES = ['', 'Secure', 'Suspicious', 'Elevated', 'High', 'Critical'];

/** Everything the old engineering dashboard showed, for the team on the LAN. */
export default function Diagnostics() {
  const session = useSession();
  const { state, stale } = useLiveState(true);
  const history = useQuery({ queryKey: ['level-history'], queryFn: () => api.get<any>('/api/level-history'), refetchInterval: 15_000 });
  if (!state) return <Spinner label="Connecting…" />;
  const tz = session.data?.timezone ?? 'Asia/Kolkata';
  const f = state.features ?? state.scores ?? {};
  const scene = state.scene ?? state.scene_classifier ?? null;
  const contributions: any[] = Array.isArray(state.contributions) ? state.contributions : [];
  const events: any[] = Array.isArray(history.data) ? history.data : history.data?.items ?? history.data?.history ?? [];
  const level = state.alarm?.level ?? state.level;
  return (
    <div className="grid gap-4">
      {stale ? <Strip tone="warn">No live update for more than 10 seconds: these values may be old.</Strip> : null}
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-[24px] font-bold">Diagnostics</h1>
        <Pill tone={level >= 3 ? 'alarm' : level === 2 ? 'warn' : 'ok'}>
          Level {level} {LEVEL_NAMES[level] ?? ''}
        </Pill>
        <span className="text-[13px] text-slate">{state.reason}</span>
      </div>
      <div className="grid gap-4 lg:grid-cols-[1.6fr_1fr]">
        <div className="grid content-start gap-4">
          <Panel title="Live picture with overlays">
            <img src="/video" alt="Live picture with detection boxes" className="w-full rounded-xl bg-hatch" />
          </Panel>
          <Panel title="Fusion">
            <KV
              rows={[
                ['Threat Confidence Index (TCI)', `${((state.tci ?? 0) * 100).toFixed(1)}%`],
                ['Leading class', state.fusion?.class ?? state.incident_type],
                ['Evidence', state.evidence_state],
                ['Rule', state.fusion?.rule ?? '—'],
                ['Modalities', Object.keys(state.fusion?.modalities ?? {}).join(' + ') || '—'],
                ['People in view', state.person_count],
                ['Recognised', state.authorized ? 'yes' : 'no'],
              ]}
            />
            {contributions.length ? (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[520px] text-left text-[12.5px]">
                  <thead className="text-slate">
                    <tr>
                      <th className="py-1 pr-3 font-semibold">Input</th>
                      <th className="py-1 pr-3 font-semibold">Effect</th>
                      <th className="py-1 font-semibold">Detail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {contributions.map((c, i) => (
                      <tr key={i} className="border-t border-line">
                        <td className="py-1 pr-3 font-semibold">{c.input ?? c.name}</td>
                        <td className="py-1 pr-3">{c.effect ?? c.role}</td>
                        <td className="py-1 text-slate">{c.detail ?? c.note ?? ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </Panel>
          <Panel title="Level changes">
            {events.length ? (
              <ol className="grid gap-1.5 text-[12.5px]">
                {events
                  .slice()
                  .reverse()
                  .slice(0, 30)
                  .map((e, i) => (
                    <li key={i} className="grid grid-cols-[140px_1fr] gap-2">
                      <time className="text-slate">{e.at ? formatDayTime(e.at, tz) : ''}</time>
                      <span>
                        <b>Level {e.level}</b> {e.reason ? `· ${e.reason}` : ''}
                      </span>
                    </li>
                  ))}
              </ol>
            ) : (
              <p className="text-[13px] text-slate">No level changes yet.</p>
            )}
          </Panel>
        </div>
        <div className="grid content-start gap-4">
          <Panel title="Engine scores">
            <div className="grid gap-1.5">
              {['vision', 'audio', 'motion', 'behaviour', 'identity', 'weapon', 'fire'].map((k) => (
                <Bar key={k} label={k.charAt(0).toUpperCase() + k.slice(1)} value={f[k]} />
              ))}
              <Bar label="Night" value={f.is_night ? 1 : 0} hint={f.is_night ? 'night' : 'day'} />
            </div>
          </Panel>
          <Panel title="Scene classifier">
            {scene && scene.probs ? (
              <div className="grid gap-1.5">
                {Object.entries(scene.probs as Record<string, number>).map(([k, v]) => (
                  <Bar key={k} label={k} value={v} />
                ))}
                {scene.thresholds ? <p className="text-[12px] text-slate">Thresholds: {Object.entries(scene.thresholds as Record<string, number>).map(([k, v]) => `${k} ${v.toFixed(2)}`).join(', ')}</p> : null}
              </div>
            ) : (
              <p className="text-[13px] text-slate">{scene?.label ?? 'Not installed.'}</p>
            )}
          </Panel>
          <Panel title="Pipeline">
            <KV
              rows={[
                ['Camera', state.camera_online ? 'sending' : 'no signal'],
                ['Video', `${(state.video_fps ?? 0).toFixed(1)} fps`],
                ['AI', `${(state.ai_hz ?? 0).toFixed(1)} Hz`],
                ['Frame age', state.frame_age_ms != null ? `${Math.round(state.frame_age_ms)} ms` : '—'],
                ['Latency avg / p95', `${Math.round(state.latency_ms ?? 0)} / ${Math.round(state.latency_p95_ms ?? 0)} ms`],
                ['Queue', state.queue_depth ?? 0],
                ['Audio', state.audio_label ?? '—'],
                ['Microphone', state.mic_delivering == null ? '—' : state.mic_delivering ? 'delivering' : 'silent'],
                ['Analysis', state.clip?.mode === 'clip' ? `4 s clips, p95 ${state.clip?.compute_ms?.p95 ?? '—'} ms` : 'tick loop'],
                ['Remote viewers', state.remote_viewers ?? 0],
              ]}
            />
          </Panel>
        </div>
      </div>
    </div>
  );
}
