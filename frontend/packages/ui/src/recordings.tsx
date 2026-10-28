import { ApiError, type Client } from '@sentrix/api';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { Button, EmptyState, Field, List, ListRow, Strip, TextInput, useToast } from './components';
import { formatBytes, formatCountdown, formatDayTime } from './format';

interface RecordingItem {
  name: string;
  label: string;
  created_at: string;
  seconds: number | null;
  bytes: number;
  has_audio: boolean;
  interrupted: boolean;
  files: { video: string | null; audio: string | null; meta: string };
}
interface RecordingStatus {
  enabled: boolean;
  active: boolean;
  recording: boolean;
  finalizing: boolean;
  name: string | null;
  label: string;
  seconds: number;
  max_seconds: number;
  bytes: number;
  audio: { present: boolean; device: string | null };
  stop_reason: string | null;
  last_error: string | null;
}
interface RecordingsPayload {
  status: RecordingStatus;
  items: RecordingItem[];
  free_bytes: number | null;
  ffmpeg: boolean;
  microphone: { device?: string | null; connected?: boolean } | null;
  keep_days: number;
  via: 'lan' | 'portal';
  can_delete: boolean;
  download_here: boolean;
  lan_url: string | null;
}

/** "Record video" (decision D20): start and stop a training recording; on the
 *  home network also list, download and delete. Through the Portal the files
 *  stay on the device and the panel says where to get them. */
export function RecordingsPanel({ client, timezone }: { client: Client; timezone: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [label, setLabel] = useState('');
  const [confirm, setConfirm] = useState<string | null>(null);
  const q = useQuery({
    queryKey: ['recordings', client.base],
    queryFn: () => client.get<RecordingsPayload>('/api/recordings'),
    refetchInterval: (query) => ((query.state.data as RecordingsPayload | undefined)?.status?.active ? 1000 : 10_000),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ['recordings', client.base] });
  const start = useMutation({
    mutationFn: () => client.post('/api/recordings/start', { label }),
    onSuccess: () => {
      setLabel('');
      refresh();
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not start recording.'),
  });
  const stop = useMutation({
    mutationFn: () => client.post('/api/recordings/stop', {}),
    onSuccess: refresh,
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not stop recording.'),
  });
  const remove = useMutation({
    mutationFn: (name: string) => client.del(`/api/recordings/${encodeURIComponent(name)}`),
    onSuccess: () => {
      setConfirm(null);
      toast('Recording deleted.');
      refresh();
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not delete it.'),
  });

  if (q.isLoading) return <p className="text-[13px] text-slate">Loading recordings…</p>;
  if (q.isError || !q.data) return <Strip tone="warn">Recordings are not available right now.</Strip>;
  const d = q.data;
  const s = d.status;
  if (!s.enabled) return <Strip tone="info">Recording is switched off on this device.</Strip>;

  function submit(e: FormEvent) {
    e.preventDefault();
    if (s.active) stop.mutate();
    else start.mutate();
  }

  const mic = s.audio?.present || d.microphone?.connected ? `with sound${s.audio?.device || d.microphone?.device ? ` (${s.audio?.device ?? d.microphone?.device})` : ''}` : 'no microphone, video only';
  return (
    <div className="grid gap-3">
      <form onSubmit={submit} noValidate className="grid gap-2.5">
        {!s.active ? (
          <Field label="What is being recorded" htmlFor="rec-label" optional help="For example: replica gun, two people. Everyone filmed must agree to it.">
            <TextInput id="rec-label" value={label} maxLength={80} onChange={(e) => setLabel(e.target.value)} placeholder="replica gun, two people" />
          </Field>
        ) : null}
        <div role="status" aria-live="polite" className="text-[13.5px]">
          {s.recording ? (
            <span className="font-semibold text-chilli">
              Recording {formatCountdown(s.seconds * 1000)} of {formatCountdown(s.max_seconds * 1000)}, {formatBytes(s.bytes)}, {mic}
            </span>
          ) : s.finalizing ? (
            'Finishing the files…'
          ) : (
            <span className="text-slate">
              Ready, {mic}{d.free_bytes != null ? `. ${formatBytes(d.free_bytes)} free` : ''}.{!d.ffmpeg ? ' Sound is saved as a separate file on this device.' : ''}
            </span>
          )}
        </div>
        {s.stop_reason && !s.active ? <Strip tone="warn">The last recording stopped because {s.stop_reason}.</Strip> : null}
        <Button type="submit" variant={s.active ? 'dark' : 'alarm'} busy={start.isPending || stop.isPending} disabled={s.finalizing}>
          {s.active ? 'Stop recording' : 'Start recording'}
        </Button>
      </form>

      {d.via === 'portal' ? (
        <Strip tone="info">
          Recordings stay on the device. Download them from a computer on the same network as the device{d.lan_url ? ` at ${d.lan_url}` : ''}.
        </Strip>
      ) : !d.download_here ? (
        <Strip tone="info">Downloads are off in this browser because it runs on the device itself. Open {d.lan_url ?? 'the device’s address'} on a laptop on the same network.</Strip>
      ) : null}

      {d.items.length === 0 ? (
        <EmptyState title="No recordings yet">Recordings are deleted after {Math.round(d.keep_days)} days.</EmptyState>
      ) : (
        <List>
          {d.items.map((it) => (
            <ListRow
              key={it.name}
              label={it.label || it.name}
              sub={`${formatDayTime(it.created_at, timezone)}. ${it.seconds != null ? formatCountdown(it.seconds * 1000) : ''}, ${formatBytes(it.bytes)}, ${it.has_audio ? 'with sound' : 'no sound'}${it.interrupted ? ', recovered after an interruption' : ''}`}
            >
              {d.via === 'lan' ? (
                <span className="flex shrink-0 flex-wrap justify-end gap-2 text-[12.5px] font-semibold">
                  {d.download_here && it.files.video ? (
                    <a className="text-indigo underline" href={`/media/recording/${encodeURIComponent(it.files.video)}`} download>
                      Video
                    </a>
                  ) : null}
                  {d.download_here && it.files.audio ? (
                    <a className="text-indigo underline" href={`/media/recording/${encodeURIComponent(it.files.audio)}`} download>
                      Sound
                    </a>
                  ) : null}
                  {d.can_delete ? (
                    <button type="button" className="text-chilli underline" onClick={() => (confirm === it.name ? remove.mutate(it.name) : setConfirm(it.name))}>
                      {confirm === it.name ? 'Confirm delete' : 'Delete'}
                    </button>
                  ) : null}
                </span>
              ) : null}
            </ListRow>
          ))}
        </List>
      )}
      <p className="text-[12px] text-slate">Recordings are deleted after {Math.round(d.keep_days)} days.</p>
    </div>
  );
}
