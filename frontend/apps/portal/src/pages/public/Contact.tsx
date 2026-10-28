import { ApiError, createClient } from '@sentrix/api';
import { Band, Button, ErrorState, List, ListRow, Spinner, Strip, formatTime } from '@sentrix/ui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { MapPin, Phone } from 'lucide-react';
import { useState } from 'react';
import { useParams } from 'react-router';
import { headline } from '../../features/alert/AlertScreen';
import type { RawRequest } from '../../lib/data';

/* Pages reached from a link in a message, without an account. Each answers
   with its own CSRF token (cookie + header). */

interface ConsentPageOut {
  contact_name: string;
  relation: string | null;
  status: string;
  owner_name: string;
  purposes: { key: string; title: string; text: string }[];
  notice_version: string;
  declined_days: number;
  csrf_token: string;
}

/** /consent/:token - the contact agrees (or not) to receive alerts. */
export function Consent() {
  const { token } = useParams();
  const q = useQuery({ queryKey: ['consent', token], queryFn: () => createClient().get<ConsentPageOut>(`/api/v2/consent/${encodeURIComponent(token!)}`), retry: false });
  const [done, setDone] = useState<{ decision: 'confirm' | 'decline'; telegram_link: string | null } | null>(null);
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState<string | null>(null);
  if (q.isLoading) return <Spinner />;
  if (!q.data) return <div className="pt-6"><ErrorState title="This link no longer works" message="Ask the person who added you to send it again." /></div>;
  const d = q.data;
  async function answer(decision: 'confirm' | 'decline') {
    setBusy(decision);
    setError(undefined);
    try {
      const r = await createClient({ csrf: () => d.csrf_token }).post<{ decision: 'confirm' | 'decline'; telegram_link: string | null }>(`/api/v2/consent/${encodeURIComponent(token!)}`, { decision });
      setDone(r);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'That did not work. Open the link again.');
    } finally {
      setBusy(null);
    }
  }
  if (done) {
    return (
      <div className="grid gap-4 pt-4">
        <h1 className="text-[24px] font-bold">{done.decision === 'confirm' ? 'Thank you' : 'You won’t get alerts'}</h1>
        {done.decision === 'confirm' ? (
          <>
            <p className="text-[14px]">If {d.owner_name} asks for help, or SENTRIX can’t reach them during an alert, you’ll get a message with what happened and where.</p>
            {done.telegram_link ? (
              <a href={done.telegram_link} target="_blank" rel="noopener" className="inline-flex justify-center rounded-xl bg-indigo-fill px-4 py-3 font-semibold text-on-fill">
                Also get alerts on Telegram
              </a>
            ) : null}
          </>
        ) : (
          <p className="text-[14px]">We erased your number and e-mail. {d.owner_name} will see that you declined.</p>
        )}
      </div>
    );
  }
  return (
    <div className="grid gap-4 pt-4">
      <h1 className="text-[24px] font-bold">{d.owner_name} added you as an emergency contact</h1>
      <p className="text-[14px]">SENTRIX watches {d.owner_name}’s home for a weapon or a fire. If they ask for help, you may get a message saying what happened and where, so you can check on them or call for help.</p>
      {d.status === 'CONFIRMED' ? <Strip tone="ok">You already confirmed. You can still opt out below.</Strip> : null}
      <List>
        <ListRow label="What you’d get" sub="The kind of alert, the room, the address and how to reach them. A picture only if they allowed it." />
        <ListRow label="How often" sub="Only when something is detected and help is needed. Nothing else." />
        <ListRow label="What SENTRIX never does" sub="It never contacts 112 itself and never shares your number." />
      </List>
      {error ? <Strip tone="alarm" role="alert">{error}</Strip> : null}
      <Button variant="primary" busy={busy === 'confirm'} onClick={() => answer('confirm')}>
        Yes, alert me
      </Button>
      <Button busy={busy === 'decline'} onClick={() => answer('decline')}>
        No, don’t alert me
      </Button>
      <a href="/privacy/notice" className="text-[13px] font-semibold text-indigo underline">
        Privacy notice (version {d.notice_version})
      </a>
    </div>
  );
}

interface ContactPageOut {
  contact_name: string;
  resident: string;
  resident_phone: string | null;
  request: RawRequest & { duress: boolean; banner: string | null };
  address: string | null;
  landmark: string | null;
  entry_note: string | null;
  map_url: string | null;
  medical: string | null;
  has_snapshot: boolean;
  snapshot_url: string | null;
  can_acknowledge: boolean;
  can_approve: boolean;
  call: { href: string | null; label: string; test_mode: boolean };
  footer: string;
  csrf_token: string;
}

/** /c/:token - an emergency contact's view of an alert. */
export function ContactAlert() {
  const { token } = useParams();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ['contact-page', token], queryFn: () => createClient().get<ContactPageOut>(`/api/v2/contact-page/${encodeURIComponent(token!)}`), retry: false, refetchInterval: 8000 });
  const [msg, setMsg] = useState<string>();
  const [busy, setBusy] = useState<string | null>(null);
  if (q.isLoading) return <Spinner />;
  if (!q.data) return <div className="pt-6"><ErrorState title="This alert page has closed" message="It works only while the alert is open, plus two hours." /></div>;
  const d = q.data;
  const r = d.request;
  const tz = 'Asia/Kolkata';
  async function act(action: 'acknowledge' | 'approve') {
    setBusy(action);
    try {
      const out = await createClient({ csrf: () => d.csrf_token }).post<{ message: string }>(`/api/v2/contact-page/${encodeURIComponent(token!)}`, { action });
      setMsg(out.message);
      qc.invalidateQueries({ queryKey: ['contact-page', token] });
    } catch (err) {
      setMsg(err instanceof ApiError ? err.message : 'That did not work.');
    } finally {
      setBusy(null);
    }
  }
  const title = r.duress ? 'Emergency: send help' : headline({ incidentType: r.incident_type ?? 'threat', evidence: r.evidence_state, room: null });
  return (
    <div className="-mx-4 grid gap-3">
      <Band tone={r.duress || r.state === 'PENDING' ? 'alarm' : 'help'} title={title} right={<span className="text-[12px] tabular">{r.created_at ? formatTime(r.created_at, tz) : ''}</span>}>
        {d.resident}’s home. Request {r.short_id}.
      </Band>
      <div className="grid gap-3 px-4">
        {r.banner ? <Strip tone="alarm">{r.banner}</Strip> : null}
        <p className="text-[14px] font-semibold">{r.label}</p>
        {d.snapshot_url ? <img src={d.snapshot_url} alt="Picture from the camera at the time of the alert" className="w-full rounded-xl bg-hatch" /> : null}
        <List>
          {d.address ? <ListRow label="Address" sub={[d.address, d.landmark].filter(Boolean).join('. ')} /> : null}
          {d.entry_note ? <ListRow label="How to find the door" sub={d.entry_note} /> : null}
          {d.medical ? <ListRow label="Medical notes" sub={d.medical} /> : null}
        </List>
        {d.map_url ? (
          <a href={d.map_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center justify-center gap-2 rounded-xl border-[1.5px] border-line bg-paper px-4 py-3 font-semibold">
            <MapPin aria-hidden className="size-4" /> Open in maps
          </a>
        ) : null}
        {d.resident_phone ? (
          <a href={`tel:${d.resident_phone}`} className="inline-flex items-center justify-center gap-2 rounded-xl border-[1.5px] border-line bg-paper px-4 py-3 font-semibold">
            <Phone aria-hidden className="size-4" /> Call {d.resident}
          </a>
        ) : r.duress ? (
          <Strip tone="alarm">Do not call {d.resident} first.</Strip>
        ) : null}
        {d.call.href ? (
          <a href={d.call.href} className="inline-flex items-center justify-center gap-2 rounded-xl bg-chilli-fill px-4 py-3 font-semibold text-on-fill">
            <Phone aria-hidden className="size-4" /> {d.call.label}
          </a>
        ) : null}
        {msg ? <Strip tone="info" role="status">{msg}</Strip> : null}
        {d.can_approve ? (
          <Button variant="alarm" busy={busy === 'approve'} onClick={() => act('approve')}>
            Alert the SENTRIX desk
          </Button>
        ) : null}
        {d.can_acknowledge ? (
          <Button variant="primary" busy={busy === 'acknowledge'} onClick={() => act('acknowledge')}>
            I’m handling it
          </Button>
        ) : null}
        <p className="text-[12px] text-slate">{d.footer}</p>
      </div>
    </div>
  );
}

/** /account/verify-email/:token */
export function VerifyEmail() {
  const { token } = useParams();
  const q = useQuery({ queryKey: ['verify', token], queryFn: () => createClient().get<{ valid: boolean }>(`/api/v2/account/verify-email/${encodeURIComponent(token!)}`), retry: false });
  const [state, setState] = useState<'idle' | 'done' | 'error'>('idle');
  const [busy, setBusy] = useState(false);
  if (q.isLoading) return <Spinner />;
  if (!q.data?.valid) return <div className="pt-6"><ErrorState title="This link no longer works" message="Sign in and send the confirmation e-mail again from your account." /></div>;
  return (
    <div className="grid gap-4 pt-4">
      <h1 className="text-[24px] font-bold">{state === 'done' ? 'E-mail confirmed' : 'Confirm your e-mail'}</h1>
      {state === 'done' ? (
        <a href="/home" className="font-semibold text-indigo underline">
          Go to SENTRIX
        </a>
      ) : (
        <Button
          variant="primary"
          busy={busy}
          onClick={async () => {
            setBusy(true);
            try {
              const session = await createClient().get<{ csrf_token: string | null; login_csrf_token: string | null }>('/api/v2/session');
              await createClient({ csrf: () => session.csrf_token ?? session.login_csrf_token ?? undefined }).post(`/api/v2/account/verify-email/${encodeURIComponent(token!)}`, {});
              setState('done');
            } catch {
              setState('error');
            } finally {
              setBusy(false);
            }
          }}
        >
          Confirm this e-mail address
        </Button>
      )}
      {state === 'error' ? <Strip tone="alarm">That did not work. The link may have been used already.</Strip> : null}
    </div>
  );
}

/** /privacy/notice */
export function PrivacyNotice() {
  const q = useQuery({ queryKey: ['notice'], queryFn: () => createClient().get<{ notice_version: string; purposes: { key: string; title: string; text: string }[]; grievance: string; grace_days: number; retention: Record<string, number> }>('/api/v2/privacy/notice') });
  if (q.isLoading) return <Spinner />;
  if (!q.data) return <ErrorState message="The notice could not be loaded." />;
  const d = q.data;
  return (
    <article className="grid gap-4 pt-4">
      <h1 className="text-[26px] font-bold">Privacy notice</h1>
      <p className="text-[13px] text-slate">Version {d.notice_version}</p>
      <p className="max-w-prose text-[14.5px] leading-relaxed">SENTRIX watches your home on a device inside it. Faces, video and evidence stay on that device. The SENTRIX server keeps your account, your home’s address, your contacts, and a record of each alert.</p>
      <h2 className="text-[18px] font-bold">What you can agree to</h2>
      <List>
        {d.purposes.map((p) => (
          <ListRow key={p.key} label={p.title} sub={p.text} />
        ))}
      </List>
      <h2 className="text-[18px] font-bold">Closing your account</h2>
      <p className="max-w-prose text-[14.5px] leading-relaxed">You can close your account from Settings, Privacy. Everything is erased after {d.grace_days} days; you can change your mind until then.</p>
      <h2 className="text-[18px] font-bold">Questions and complaints</h2>
      <p className="max-w-prose text-[14.5px] leading-relaxed">{d.grievance}</p>
    </article>
  );
}
