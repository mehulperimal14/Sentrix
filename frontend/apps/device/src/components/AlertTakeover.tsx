import { ApiError } from '@sentrix/api';
import { Band, BandButton, Button, Countdown, PinPad, Segmented, Strip, formatTime, useToast } from '@sentrix/ui';
import { useMutation } from '@tanstack/react-query';
import { Settings } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router';
import { api, type DeviceSession } from '../lib/api';
import { alertHeadline, defaultAuthority, evidencePhrase, type SosBlock } from '../lib/state';

type Authority = 'POLICE' | 'FIRE' | 'MEDICAL';
const HELP_PATH = ['APPROVED', 'DISPATCHED', 'ACKNOWLEDGED', 'FAILED'];

/** The alert screen on the device: same headline, answers and labels as the
 *  owner's phone (SOS contract labels, docs/7 §7.10), readable across a room,
 *  with an on-screen PIN pad. */
export function AlertTakeover({ sos, session }: { sos: SosBlock; session: DeviceSession }) {
  const toast = useToast();
  const [authority, setAuthority] = useState<Authority>((sos.recommended_authority as Authority) || defaultAuthority(sos.incident_type));
  const [pinFor, setPinFor] = useState<null | 'decline' | 'safe'>(null);
  const [pinError, setPinError] = useState<string>();
  const [showCall, setShowCall] = useState(false);
  const room = session.device.room;
  const id = sos.request_id!;
  const state = sos.state ?? 'PENDING';
  const pending = state === 'PENDING' || state === 'DRAFTED';
  const failed = state === 'FAILED';
  const help = HELP_PATH.includes(state);

  const act = useMutation({
    mutationFn: ({ action, body }: { action: string; body?: Record<string, unknown> }) => api.post(`/api/sos/requests/${id}/${action}`, body ?? {}),
    onSuccess: (_d, v) => {
      setPinFor(null);
      setPinError(undefined);
      if (v.action === 'checking') toast('Reminders paused for 5 minutes. The alert still lapses on time.');
    },
    onError: (e: unknown, v) => {
      const msg = e instanceof ApiError ? e.message : 'That did not work. Try again.';
      if (v.action === 'decline' || v.action === 'safe') setPinError(msg);
      else toast(msg);
    },
  });

  const can = (a: string) => sos.actions?.includes(a);

  const band = pending ? (
    <Band
      tone="alarm"
      title="Needs an answer"
      right={sos.expires_at ? <Countdown until={sos.expires_at} className="font-display text-[34px] font-bold leading-none stretch-82" /> : null}
    >
      <div className="flex items-center justify-between gap-3">
        <span className="text-[14px]">
          {sos.expires_at ? `Answer by ${formatTime(sos.expires_at, session.timezone)}. ` : ''}The owner has been asked by phone too.
        </span>
        {can('call_112') ? <BandButton onClick={() => setShowCall(true)}>{session.call.label}</BandButton> : null}
      </div>
    </Band>
  ) : failed ? (
    <Band tone="alarm" title={`Not delivered. ${session.call.label}`} right={<BandButton onClick={() => setShowCall(true)}>{session.call.label}</BandButton>} />
  ) : help ? (
    <Band tone="help" title={sos.label ?? 'Help requested'} right={can('call_112') ? <BandButton onClick={() => setShowCall(true)}>{session.call.label}</BandButton> : null} />
  ) : (
    <Band tone="quiet" title={sos.label ?? 'Closed'} />
  );

  return (
    <div className="flex h-full flex-col" role="alertdialog" aria-label={alertHeadline(sos.incident_type, sos.evidence_state, room)}>
      {band}
      <div className="grid min-h-0 flex-1 gap-5 p-5 lg:grid-cols-[1.15fr_1fr]">
        <div className="flex min-h-0 flex-col gap-2">
          <h1 className="font-display text-[clamp(26px,3.4vw,40px)] font-bold stretch-90">{alertHeadline(sos.incident_type, sos.evidence_state, room)}</h1>
          <p className="text-[15px] text-slate">
            {sos.created_at ? `${formatTime(sos.created_at, session.timezone)}. ` : ''}
            {evidencePhrase(sos.evidence_state)}.
          </p>
          {sos.snapshot ? (
            <img src={`/${sos.snapshot.replace(/^\//, '')}`} alt="Picture from the moment it was seen" className="min-h-0 w-full flex-1 rounded-xl bg-hatch object-cover" />
          ) : (
            <div className="grid flex-1 place-items-center rounded-xl bg-hatch text-[13px] text-slate">No picture</div>
          )}
        </div>
        <div className="flex min-h-0 flex-col gap-2.5 overflow-y-auto">
          {showCall ? (
            <div className="grid gap-3">
              <h2 className="font-sans text-[18px] font-bold">{session.call.label}</h2>
              <p className="text-[14px] text-slate">
                {session.call.number
                  ? `Dial ${session.call.number} from any phone.`
                  : session.testMode
                    ? 'Calling is off in this demo.'
                    : 'Dial 112 from any phone.'}{' '}
                SENTRIX does not contact 112 itself.
              </p>
              <Button onClick={() => setShowCall(false)}>Close</Button>
            </div>
          ) : pinFor ? (
            <PinPad
              large
              title={pinFor === 'decline' ? 'Enter the SOS PIN to mark this as a false alarm' : 'Enter the SOS PIN to say you are safe'}
              error={pinError}
              busy={act.isPending}
              onCancel={() => {
                setPinFor(null);
                setPinError(undefined);
              }}
              onSubmit={(pin) => act.mutate({ action: pinFor, body: { pin } })}
            />
          ) : (
            <>
              {can('approve') ? (
                <>
                  <Segmented<Authority>
                    label="Who is needed"
                    value={authority}
                    onChange={setAuthority}
                    options={[
                      { value: 'POLICE', label: 'Police' },
                      { value: 'FIRE', label: 'Fire' },
                      { value: 'MEDICAL', label: 'Medical' },
                    ]}
                  />
                  <Button variant="alarm" size="lg" busy={act.isPending && act.variables?.action === 'approve'} onClick={() => act.mutate({ action: 'approve', body: { authority } })} sub="Contacts and the SENTRIX Desk get the message">
                    Alert my contacts and the SENTRIX desk
                  </Button>
                </>
              ) : null}
              {can('checking') ? (
                <Button onClick={() => act.mutate({ action: 'checking' })} sub="Pauses reminders for 5 minutes">
                  I&apos;m checking
                </Button>
              ) : null}
              {can('decline') ? (
                <Button variant="quiet" onClick={() => setPinFor('decline')}>
                  False alarm (PIN)
                </Button>
              ) : null}
              {can('safe') ? (
                <Button variant="quiet" onClick={() => setPinFor('safe')}>
                  I&apos;m safe now (PIN)
                </Button>
              ) : null}
              {can('help_now') ? (
                <Button variant="alarm" onClick={() => act.mutate({ action: 'help-now', body: { authority } })} sub="Opens a new help request">
                  Request help now
                </Button>
              ) : null}
              {help ? <Strip tone="info">{sos.label}. Help stays open until the SENTRIX Desk closes it.</Strip> : null}
              <p className="text-[12.5px] text-slate">SENTRIX does not contact 112 itself.</p>
            </>
          )}
          {session.canAdmin ? (
            // The alert covers the wall view and its admin button; the device
            // admin can still reach the admin pages (the alert keeps running).
            <Link to="/admin" className="mt-auto inline-flex items-center gap-1.5 self-end rounded-lg border border-line px-2.5 py-1 text-[13px] font-semibold text-graphite">
              <Settings className="size-4" aria-hidden />
              Device admin
            </Link>
          ) : null}
        </div>
      </div>
    </div>
  );
}
