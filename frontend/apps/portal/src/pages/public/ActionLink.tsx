import { ErrorState, Spinner, Strip } from '@sentrix/ui';
import { createClient } from '@sentrix/api';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router';
import { AlertScreen } from '../../features/alert/AlertScreen';
import { toCall, toHelpRequest, type RawRequest } from '../../lib/data';
import type { Authority } from '../../lib/models';

interface ActionLinkOut {
  name: string | null;
  request: RawRequest;
  can_act: boolean;
  call: { href: string | null; label: string; test_mode: boolean };
  authorities: { value: string; label: string }[];
  default_authority: string;
  csrf_token: string;
}

/** /a/:token - the owner answers from an e-mail or message link without
 *  signing in. A link can only ask for help, never cancel it (docs/7): the
 *  false alarm needs the signed-in screen and the PIN. */
export function ActionLink() {
  const { token } = useParams();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ['action', token], queryFn: () => createClient().get<ActionLinkOut>(`/api/v2/action/${encodeURIComponent(token!)}`), retry: false, refetchInterval: 5000 });
  if (q.isLoading) return <Spinner />;
  if (q.isError || !q.data)
    return (
      <div className="grid gap-3 pt-6">
        <ErrorState title="This link no longer works" message="It may be too old or the alert has closed. Sign in to see the alert." />
        <Link to="/login" className="font-semibold text-indigo underline">
          Sign in
        </Link>
      </div>
    );
  const d = q.data;
  const client = createClient({ csrf: () => d.csrf_token });
  const request = toHelpRequest(d.request, { defaultAuthority: d.default_authority });
  request.actions = d.can_act ? request.actions.filter((a) => a === 'approve' || a === 'checking') : [];
  return (
    <div className="-mx-4 grid gap-3">
      <AlertScreen
        tokenView
        request={request}
        call={toCall(d.call, d.call.test_mode)}
        timezone="Asia/Kolkata"
        answer={async (action, body?: { authority?: Authority }) => {
          if (action !== 'approve' && action !== 'checking') return;
          await client.post(`/api/v2/action/${encodeURIComponent(token!)}`, { action, authority: body?.authority });
          await qc.invalidateQueries({ queryKey: ['action', token] });
        }}
      />
      <div className="px-4">
        <Strip tone="info">
          To mark it as a false alarm, <Link to={`/login?next=${encodeURIComponent(`/alert/${d.request.id}`)}`} className="font-semibold underline">sign in</Link> and use your SOS PIN. A link can only ask for help, never cancel it.
        </Strip>
      </div>
    </div>
  );
}
