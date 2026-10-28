import { Countdown } from '@sentrix/ui';
import { Link, useLocation } from 'react-router';
import { useOpenRequests } from '../../lib/data';
import { useSession } from '../../lib/api';
import { headline } from './AlertScreen';

/** A red bar on every owner and member page while an alert needs attention
 *  (waiting for an answer, or not delivered). */
export function OpenAlertBar() {
  const { data: s } = useSession();
  const location = useLocation();
  const enabled = Boolean(s?.signedIn && (s.role === 'owner' || s.role === 'member'));
  const { data } = useOpenRequests(enabled);
  const req = data?.requests?.find((r) => ['PENDING', 'DRAFTED', 'FAILED'].includes(r.state));
  if (!req || location.pathname.startsWith(`/alert/${req.id}`)) return null;
  const failed = req.state === 'FAILED';
  return (
    <div role="alert" className="sticky top-0 z-40 flex items-center justify-between gap-3 bg-chilli-fill px-4 py-2 text-[13px] font-bold text-on-fill safe-x">
      <span>
        {failed ? 'Your help request was not delivered.' : `${headline({ incidentType: req.incident_type ?? 'threat', evidence: req.evidence_state, room: null })}. Needs your answer`}
        {!failed && req.expires_at ? (
          <>
            {' '}
            <Countdown until={req.expires_at} />
          </>
        ) : null}
      </span>
      <Link to={`/alert/${req.id}`} className="rounded-lg border-[1.5px] border-on-fill px-3 py-0.5 text-[12.5px]">
        {failed ? 'Open' : 'Answer'}
      </Link>
    </div>
  );
}
