import { ApiError } from '@sentrix/api';
import {
  Band,
  BandButton,
  Button,
  Countdown,
  List,
  ListRow,
  PinPad,
  Segmented,
  Sheet,
  Strip,
  capitalise,
  formatTime,
  useToast,
} from '@sentrix/ui';
import { useState } from 'react';
import type { AnswerAction, Authority, CallInfo, HelpRequest } from '../../lib/models';

const HELP = ['APPROVED', 'DISPATCHED', 'ACKNOWLEDGED'];
const NOUN: Record<string, string> = { weapon: 'weapon', fire: 'fire', assault: 'fight', intrusion: 'intruder', duress: 'emergency' };

export function headline(r: Pick<HelpRequest, 'incidentType' | 'evidence' | 'room'>): string {
  const noun = NOUN[r.incidentType] ?? 'threat';
  const where = r.room ? ` in the ${r.room}` : '';
  return r.evidence === 'corroborated' ? `${capitalise(noun)} confirmed${where}` : `Possible ${noun}${where}`;
}

export function evidenceText(evidence: string | null): string {
  if (evidence === 'corroborated') return 'Confirmed by two different models';
  if (evidence === 'degraded') return 'Camera view degraded (dark), detection unreliable';
  if (evidence === 'simulated') return 'SIMULATED DETECTION (test)';
  return 'Possible (one camera check)';
}

export interface AlertScreenProps {
  request: HelpRequest;
  call: CallInfo;
  timezone: string;
  /** Sends one answer; resolves on success, throws ApiError otherwise. */
  answer: (action: AnswerAction, body?: { authority?: Authority; pin?: string }) => Promise<unknown>;
  onBack?: () => void;
  onLive?: () => void;
  /** A message link without sign-in: no live view, no history. */
  tokenView?: boolean;
}

/** The alert screen (Experience Plan, journey "An alert, answered"): what,
 *  where, when, the snapshot and the SOS contract's three answers inside the
 *  first screen of a phone; everything else below the fold. */
export function AlertScreen({ request: r, call, timezone, answer, onBack, onLive, tokenView }: AlertScreenProps) {
  const toast = useToast();
  const [authority, setAuthority] = useState<Authority>(r.authority ?? r.recommendedAuthority ?? (r.incidentType === 'fire' ? 'FIRE' : 'POLICE'));
  const [pinFor, setPinFor] = useState<null | 'decline' | 'safe'>(null);
  const [pinError, setPinError] = useState<string>();
  const [busy, setBusy] = useState<AnswerAction | null>(null);
  const [callOpen, setCallOpen] = useState(false);
  const can = (a: AnswerAction) => r.actions.includes(a);
  const pending = r.state === 'PENDING' || r.state === 'DRAFTED';
  const help = HELP.includes(r.state);
  const checking = r.checkingUntil && Date.parse(r.checkingUntil) > Date.now();

  async function run(action: AnswerAction, body?: { authority?: Authority; pin?: string }) {
    setBusy(action);
    try {
      await answer(action, body);
      setPinFor(null);
      setPinError(undefined);
      if (action === 'checking') toast(`Reminders paused for 5 minutes.${r.expiresAt ? ` The alert still lapses at ${formatTime(r.expiresAt, timezone)}.` : ''}`);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'That did not work. Try again.';
      if (action === 'decline' || action === 'safe') setPinError(msg);
      else toast(msg);
    } finally {
      setBusy(null);
    }
  }

  const back = onBack ? (
    <button type="button" onClick={onBack} className="text-[13px] font-semibold underline">
      Back to Home
    </button>
  ) : null;
  const callBtn = can('call_112') || pending || help || r.state === 'FAILED' ? <BandButton onClick={() => setCallOpen(true)}>{call.label}</BandButton> : null;

  let band;
  if (pending) {
    band = (
      <Band tone="alarm" title="Needs your answer" right={r.expiresAt ? <Countdown until={r.expiresAt} className="font-display text-[30px] font-bold leading-none stretch-82" /> : null}>
        <div className="flex items-center justify-between gap-2">
          <span className="text-[12.5px]">{r.expiresAt ? `Answer by ${formatTime(r.expiresAt, timezone)}` : ''}</span>
          {callBtn}
        </div>
      </Band>
    );
  } else if (r.state === 'FAILED') {
    band = <Band tone="alarm" title="Not delivered" right={<span className="text-[12.5px]">{r.approvedAt ? formatTime(r.approvedAt, timezone) : ''}</span>}>{back}</Band>;
  } else if (help) {
    band = (
      <Band tone="help" title="Help requested" right={<span className="text-[12.5px]">{r.approvedAt ? formatTime(r.approvedAt, timezone) : ''}</span>}>
        <div className="flex items-center justify-between gap-2">
          {back ?? <span />}
          {callBtn}
        </div>
      </Band>
    );
  } else if (r.state === 'CLOSED') {
    band = <Band tone="calm" title="Closed">{back}</Band>;
  } else {
    band = <Band tone="quiet" title={r.state === 'EXPIRED' ? 'Unanswered' : r.label}>{back}</Band>;
  }

  const title = headline(r);
  return (
    <div className="flex flex-col">
      {band}
      <div className="grid gap-2.5 px-4 py-3 safe-x md:mx-auto md:w-full md:max-w-xl">
        {pending ? (
          <>
            <h1 className="text-[26px] font-bold stretch-90">{title}</h1>
            <p className="text-[13.5px] text-slate">
              Today, {formatTime(r.createdAt, timezone)}. {evidenceText(r.evidence)}.
            </p>
            {r.safetyNote ? <Strip tone={r.safetyNote.includes('was alerted') ? 'warn' : 'info'}>{r.safetyNote}</Strip> : null}
            <Snapshot url={r.snapshotUrl} onLive={tokenView ? undefined : onLive} />
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
                <Button variant="alarm" size="lg" busy={busy === 'approve'} onClick={() => run('approve', { authority })} sub="Your contacts and the SENTRIX Desk get the message">
                  Alert my contacts and the SENTRIX desk
                </Button>
              </>
            ) : null}
            {can('checking') ? (
              <Button aria-pressed={Boolean(checking)} busy={busy === 'checking'} onClick={() => run('checking')} sub={checking ? `Reminders paused until ${formatTime(r.checkingUntil!, timezone)}` : 'Pauses reminders for 5 minutes'}>
                I&apos;m checking
              </Button>
            ) : null}
            {can('decline') ? (
              <Button variant="quiet" onClick={() => setPinFor('decline')}>
                False alarm (PIN)
              </Button>
            ) : null}
            {onBack ? (
              <button type="button" onClick={onBack} className="justify-self-start text-[13px] font-semibold text-indigo underline">
                Back to Home (the alert keeps waiting)
              </button>
            ) : null}
          </>
        ) : r.state === 'FAILED' ? (
          <>
            <h1 className="text-[24px] font-bold stretch-90">Your help request has not reached anyone yet</h1>
            <p className="text-[13.5px] text-slate">Nobody outside the home has confirmed it. {call.label}.</p>
            <Button variant="alarm" size="lg" onClick={() => setCallOpen(true)}>
              {call.label}
            </Button>
            <Delivery r={r} />
          </>
        ) : help ? (
          <>
            <h1 className="text-[24px] font-bold stretch-90">{r.state === 'ACKNOWLEDGED' ? 'The SENTRIX Desk is handling it' : r.approvedBy && r.approvedBy !== 'you' ? `${r.approvedBy} asked the SENTRIX Desk for help` : 'We’re alerting your contacts and the SENTRIX Desk'}</h1>
            <p className="text-[13.5px] text-slate">
              {capitalise(title.replace(' in the ', ', '))}, {formatTime(r.createdAt, timezone)}.{r.linked ? ' Linked to the earlier alert.' : ''}
            </p>
            <Delivery r={r} />
            {r.residentSafe ? (
              <Strip tone="ok">
                <b>You’re marked safe.</b> Help stays open until the SENTRIX Desk closes it.
              </Strip>
            ) : null}
            <p className="text-[12px] text-slate">SENTRIX does not contact 112 itself. If you are in danger, {call.label.replace(/^Call/, 'call')}.</p>
            {onLive && !tokenView ? <Button onClick={onLive}>Open live view</Button> : null}
            {can('safe') && !r.residentSafe ? (
              <Button variant="quiet" onClick={() => setPinFor('safe')}>
                I&apos;m safe now (PIN)
              </Button>
            ) : null}
          </>
        ) : r.state === 'EXPIRED' ? (
          <>
            <h1 className="text-[24px] font-bold stretch-90">Unanswered{r.expiresAt ? ` at ${formatTime(r.expiresAt, timezone)}` : ''}</h1>
            <p className="text-[13.5px] text-slate">Nobody was told your home is safe.</p>
            <Delivery r={r} />
            {can('help_now') ? (
              <Button variant="alarm" size="lg" busy={busy === 'help_now'} onClick={() => run('help_now', { authority })} sub="Opens a new help request linked to this alert">
                Request help now
              </Button>
            ) : null}
            {onLive && !tokenView ? <Button onClick={onLive}>Open live view</Button> : null}
          </>
        ) : r.state === 'CLOSED' ? (
          <>
            <h1 className="text-[24px] font-bold stretch-90">Closed by the SENTRIX Desk</h1>
            {r.outcome ? <p className="text-[13.5px] text-slate">{r.outcome}</p> : null}
            <Delivery r={r} />
          </>
        ) : (
          <>
            <h1 className="text-[24px] font-bold stretch-90">{r.label}</h1>
            <p className="text-[13.5px] text-slate">If anyone was alerted, they are told it is resolved.</p>
            {can('help_now') ? (
              <Button busy={busy === 'help_now'} onClick={() => run('help_now', { authority })} sub="Opens a new help request linked to this alert">
                Request help now
              </Button>
            ) : null}
          </>
        )}
      </div>

      {pending && !tokenView ? (
        <div className="mt-2 grid gap-3 border-t-8 border-chalk bg-paper px-4 pb-6 pt-3 safe-x md:mx-auto md:w-full md:max-w-xl">
          <h2 className="font-sans text-[15px] font-bold">What each answer does</h2>
          <dl className="grid gap-2 text-[13px]">
            <dt className="font-bold">Alert my contacts and the SENTRIX desk</dt>
            <dd className="text-slate">Your confirmed contacts get a message with your address and this picture. The SENTRIX Desk gets the full request.</dd>
            <dt className="font-bold">I&apos;m checking</dt>
            <dd className="text-slate">Pauses reminders for 5 minutes. The alert still lapses on time, and the safety net still runs.</dd>
            <dt className="font-bold">False alarm (PIN)</dt>
            <dd className="text-slate">Closes the alert. Anyone already alerted is told it is resolved.</dd>
          </dl>
          {r.sosText ? (
            <details className="rounded-xl border border-line px-3 py-2 text-[13px]">
              <summary className="cursor-pointer font-semibold">What your contacts would get</summary>
              <pre className="mt-2 whitespace-pre-wrap font-mono text-[11.5px] text-slate">{r.sosText}</pre>
            </details>
          ) : null}
          <History r={r} timezone={timezone} />
          <p className="text-[12px] text-slate">SENTRIX does not contact 112 itself.</p>
        </div>
      ) : !tokenView && r.history.length ? (
        <div className="mt-2 border-t-8 border-chalk px-4 pb-6 pt-3 safe-x md:mx-auto md:w-full md:max-w-xl">
          <History r={r} timezone={timezone} />
        </div>
      ) : null}

      <Sheet open={pinFor !== null} onClose={() => setPinFor(null)}>
        <PinPad
          title={pinFor === 'safe' ? 'Enter your SOS PIN to say you are safe' : 'Enter your SOS PIN to mark this as a false alarm'}
          error={pinError}
          busy={busy === 'decline' || busy === 'safe'}
          onCancel={() => {
            setPinFor(null);
            setPinError(undefined);
          }}
          onSubmit={(pin) => run(pinFor!, { pin })}
        />
      </Sheet>
      <Sheet open={callOpen} onClose={() => setCallOpen(false)} title={call.label}>
        <p className="text-[14px] text-slate">{call.note ?? 'Your phone starts the call. SENTRIX does not contact 112 itself.'}</p>
        {call.number ? (
          <a href={`tel:${call.number.replace(/\s/g, '')}`} className="rounded-xl bg-chilli-fill px-4 py-3 text-center text-[17px] font-bold text-on-fill">
            {call.number}
          </a>
        ) : null}
        <Button onClick={() => setCallOpen(false)}>Close</Button>
      </Sheet>
    </div>
  );
}

function Snapshot({ url, onLive }: { url: string | null; onLive?: () => void }) {
  if (!url) return null;
  const img = <img src={url} alt="Snapshot from the moment it was seen" className="aspect-[16/6] w-full rounded-xl bg-hatch object-cover" />;
  return onLive ? (
    <button type="button" onClick={onLive} className="relative block w-full overflow-hidden rounded-xl" aria-label="Snapshot. Open live view">
      {img}
      <span className="absolute bottom-2 left-2 rounded-md bg-black/55 px-2 py-0.5 text-[11px] font-semibold text-white">Snapshot. Tap for live view</span>
    </button>
  ) : (
    img
  );
}

function Delivery({ r }: { r: HelpRequest }) {
  if (!r.contacts.length && !r.desk) return null;
  return (
    <List>
      {r.contacts.map((c) => (
        <ListRow key={c.name} label={c.name} value={c.status} tone={c.tone} />
      ))}
      {r.desk ? <ListRow label="SENTRIX Desk" value={r.desk.status} tone={r.desk.tone} /> : null}
      {r.authority && HELP.includes(r.state) ? <ListRow label="You asked for" value={capitalise(r.authority.toLowerCase())} /> : null}
    </List>
  );
}

function History({ r, timezone }: { r: HelpRequest; timezone: string }) {
  if (!r.history.length) return null;
  return (
    <>
      <h2 className="mt-2 font-sans text-[15px] font-bold">History</h2>
      <ol className="grid gap-1 text-[12.5px]">
        {r.history.map((h, i) => (
          <li key={i} className="grid grid-cols-[64px_1fr] gap-1.5">
            <time className="text-slate tabular">{formatTime(h.at, timezone)}</time>
            <span>{h.text}</span>
          </li>
        ))}
      </ol>
    </>
  );
}
