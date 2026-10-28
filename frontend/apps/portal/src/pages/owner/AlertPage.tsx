import { ErrorState, Spinner } from '@sentrix/ui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router';
import { AlertScreen } from '../../features/alert/AlertScreen';
import { api, useSession } from '../../lib/api';
import { toCall, toHelpRequest, useHome, type SosDetailOut } from '../../lib/data';
import type { AnswerAction, Authority } from '../../lib/models';

/** /alert/:id (and the old /sos/:id): the signed-in alert screen. */
export function AlertPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { data: s } = useSession();
  const home = useHome();
  const q = useQuery({
    queryKey: ['sos', id],
    queryFn: () => api.get<SosDetailOut>(`/api/v2/sos/${encodeURIComponent(id!)}`),
    refetchInterval: 4000,
  });
  if (q.isLoading) return <Spinner />;
  if (q.isError || !q.data) return <div className="p-4"><ErrorState title="This alert is not available" message="It may belong to another household or be too old." /></div>;
  const d = q.data;
  const room = home.data?.devices.find((x) => x.id === d.request.device_id)?.room ?? null;
  const request = toHelpRequest(d.request, { events: d.events, snapshotUrl: d.snapshot_url, room, defaultAuthority: d.default_authority });
  async function answer(action: AnswerAction, body?: { authority?: Authority; pin?: string }) {
    if (action === 'call_112') return;
    await api.post(`/api/v2/sos/${encodeURIComponent(id!)}/answer`, { action, authority: body?.authority, pin: body?.pin });
    await Promise.all([qc.invalidateQueries({ queryKey: ['sos', id] }), qc.invalidateQueries({ queryKey: ['sos-open'] }), qc.invalidateQueries({ queryKey: ['home'] })]);
  }
  return (
    <AlertScreen
      request={request}
      call={toCall(d.call, s?.testMode ?? true)}
      timezone={s?.timezone ?? 'Asia/Kolkata'}
      answer={answer}
      onBack={() => navigate('/home')}
      onLive={d.request.device_id ? () => navigate(`/live/${d.request.device_id}`) : undefined}
    />
  );
}
