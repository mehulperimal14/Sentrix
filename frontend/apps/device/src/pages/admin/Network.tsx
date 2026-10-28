import { ApiError } from '@sentrix/api';
import { Button, Spinner, formatDayTime, useToast } from '@sentrix/ui';
import { useMutation, useQuery } from '@tanstack/react-query';
import { api, useSession } from '../../lib/api';
import { KV, Panel, yesNo } from './ui';

/** The Portal link, the alert channels and who viewed the device remotely. */
export default function Network() {
  const tz = useSession().data?.timezone ?? 'Asia/Kolkata';
  const toast = useToast();
  const link = useQuery({ queryKey: ['link'], queryFn: () => api.get<any>('/api/link'), refetchInterval: 5000 });
  const channels = useQuery({ queryKey: ['channels'], queryFn: () => api.get<any>('/api/channels'), refetchInterval: 10_000 });
  const audit = useQuery({ queryKey: ['remote-audit'], queryFn: () => api.get<any>('/api/link/remote-audit') });
  const reconnect = useMutation({
    mutationFn: () => api.post('/api/link/reconnect', {}),
    onSuccess: () => toast('Reconnecting to the Portal.'),
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not reconnect.'),
  });
  const testMsg = useMutation({
    mutationFn: () => api.post<any>('/api/channels/test', {}),
    onSuccess: (r) => toast(r?.message ?? 'Test message queued (test mode: only the test list).'),
    onError: (e) => toast(e instanceof ApiError ? e.message : 'The test message failed.'),
  });
  if (link.isLoading) return <Spinner />;
  const l = link.data ?? {};
  const c = channels.data ?? {};
  const rows: any[] = audit.data?.items ?? audit.data?.audit ?? audit.data ?? [];
  return (
    <div className="grid gap-4">
      <h1 className="text-[24px] font-bold">Network</h1>
      <div className="grid gap-4 md:grid-cols-2">
        <Panel title="Portal link" actions={<Button size="sm" block={false} busy={reconnect.isPending} onClick={() => reconnect.mutate()}>Reconnect</Button>}>
          <KV
            rows={[
              ['Enabled', yesNo(l.enabled)],
              ['State', l.state ?? '—'],
              ['Connected', yesNo(l.connected)],
              ['Added to a home', yesNo(l.claimed)],
              ['Portal', l.portal ?? '—'],
              ['Last message', l.last_message_at ? formatDayTime(l.last_message_at, tz) : '—'],
              ['Waiting to send', l.outbox_depth ?? 0],
              ['Profile version', l.profile_version ?? '—'],
            ]}
          />
        </Panel>
        <Panel title="Alert channels" actions={<Button size="sm" block={false} busy={testMsg.isPending} onClick={() => testMsg.mutate()}>Send a test</Button>}>
          <KV
            rows={[
              ['Router running', yesNo(c.enabled)],
              ['Anything can leave', yesNo(c.outbound_ready)],
              ['Live sending', yesNo(c.live)],
              ...Object.entries((c.channels ?? {}) as Record<string, any>).map(([k, v]) => [`Channel ${k}`, typeof v === 'object' ? v?.status ?? yesNo(v?.ready) : yesNo(v)] as [string, string]),
              ['Modem', c.modem ? `${c.modem.registered ? 'registered' : 'not registered'}${c.modem.signal_dbm != null ? `, ${c.modem.signal_dbm} dBm` : ''}` : 'none'],
              ['Last error', c.last_error ?? '—'],
            ]}
          />
        </Panel>
      </div>
      <Panel title="Remote viewing (who watched through the Portal)">
        {Array.isArray(rows) && rows.length ? (
          <ol className="grid gap-1 text-[12.5px]">
            {rows.slice(0, 50).map((r: any, i: number) => (
              <li key={i} className="grid grid-cols-[150px_1fr] gap-2">
                <time className="text-slate">{r.at || r.created_at ? formatDayTime(r.at ?? r.created_at, tz) : ''}</time>
                <span>
                  {r.actor ?? r.who ?? 'someone'}: {r.path ?? r.what ?? r.kind ?? ''}
                </span>
              </li>
            ))}
          </ol>
        ) : (
          <p className="text-[13px] text-slate">Nobody has viewed this device remotely.</p>
        )}
      </Panel>
    </div>
  );
}
