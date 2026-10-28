import { Segmented, Spinner, StatusCard, formatTime, cn, useToast, PinPad } from '@sentrix/ui';
import { ApiError } from '@sentrix/api';
import { useMutation } from '@tanstack/react-query';
import { Settings } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { AlertTakeover } from '../components/AlertTakeover';
import { api, useLiveState, useSession } from '../lib/api';
import { levelWords } from '../lib/state';

/** The device screen: big status, live picture, mode; the alert takes over the
 *  whole screen while one is open. Readable from across the room. */
export function WallView() {
  const session = useSession();
  const { state, stale, lastAt } = useLiveState(Boolean(session.data?.signedIn));
  const toast = useToast();
  const [pinForHome, setPinForHome] = useState(false);
  const [pinError, setPinError] = useState<string>();
  const [dismissedRequest, setDismissedRequest] = useState<string | null>(null);

  const mode = useMutation({
    mutationFn: (body: { armed: boolean; pin?: string }) => api.post('/api/mode', body),
    onSuccess: (_d, v) => {
      setPinForHome(false);
      setPinError(undefined);
      toast(v.armed ? 'Away mode on.' : 'Home mode on.');
    },
    onError: (e: unknown, v) => {
      const msg = e instanceof ApiError ? e.message : 'That did not work.';
      if (!v.armed) setPinError(msg);
      else toast(msg);
    },
  });

  useEffect(() => {
    document.title = 'SENTRIX device';
  }, []);

  if (session.isLoading || !session.data) return <Spinner />;
  const s = session.data;
  if (!state) return <Spinner label="Connecting to the camera…" />;

  const sos = state.sos;
  const open = sos?.active && sos.request_id && sos.request_id !== dismissedRequest;
  if (open && !stale) {
    const terminal = ['DECLINED', 'EXPIRED', 'CANCELLED', 'CLOSED'].includes(sos.state ?? '');
    return (
      <div className="flex h-dvh flex-col bg-chalk">
        <AlertTakeover sos={sos} session={s} />
        {terminal ? (
          <button type="button" className="m-4 self-start text-[14px] font-semibold text-indigo underline" onClick={() => setDismissedRequest(sos.request_id)}>
            Back to the home view
          </button>
        ) : null}
      </div>
    );
  }

  const words = levelWords(state);
  const armed = Boolean(state.mode?.armed);
  return (
    <div className="grid h-dvh grid-rows-[1fr_auto] gap-4 bg-chalk p-4 lg:grid-cols-[1.55fr_1fr] lg:grid-rows-1">
      <div className="relative min-h-0 overflow-hidden rounded-2xl bg-hatch">
        {state.camera_online ? (
          <img src="/video" alt={`Live picture of the ${s.device.room ?? 'room'}`} className="h-full w-full object-cover" />
        ) : (
          <div className="grid h-full place-items-center text-[15px] text-slate">The camera is not sending a picture.</div>
        )}
        {state.remote_viewers > 0 ? (
          <span className="absolute right-3 top-3 rounded-md bg-chilli-fill px-2 py-0.5 text-[12px] font-bold text-on-fill">
            {state.remote_viewers === 1 ? 'Someone is watching remotely' : `${state.remote_viewers} people are watching remotely`}
          </span>
        ) : null}
        {state.recording?.active ? (
          <span className="absolute left-3 top-3 rounded-md bg-chilli-fill px-2 py-0.5 text-[12px] font-bold text-on-fill">REC</span>
        ) : null}
      </div>
      <aside className="flex min-h-0 flex-col gap-3">
        {stale ? (
          <StatusCard tone="off" title="Not connected">
            No update since {lastAt ? formatTime(lastAt, s.timezone) : 'start-up'}. What you see may be out of date.
          </StatusCard>
        ) : (
          <StatusCard tone={words.tone} title={words.title}>
            {words.tone === 'ok' ? `${armed ? 'Away' : 'Home'} mode. Nothing unusual right now.` : words.tone === 'warn' ? 'Shown here and on the owner’s phone only. Nobody else was told.' : state.reason}
          </StatusCard>
        )}
        {pinForHome ? (
          <div className="rounded-2xl bg-paper p-4">
            <PinPad
              large
              title="Enter the SOS PIN to switch to Home"
              error={pinError}
              busy={mode.isPending}
              onCancel={() => {
                setPinForHome(false);
                setPinError(undefined);
              }}
              onSubmit={(pin) => mode.mutate({ armed: false, pin })}
            />
          </div>
        ) : (
          <Segmented
            label="Mode"
            value={armed ? 'away' : 'home'}
            disabled={mode.isPending || stale}
            onChange={(v) => (v === 'away' ? mode.mutate({ armed: true }) : setPinForHome(true))}
            options={[
              { value: 'home', label: 'Home' },
              { value: 'away', label: 'Away' },
            ]}
          />
        )}
        <div className="text-[14px] text-slate">
          <b className="block text-graphite">{s.device.room ? `${s.device.room} camera` : 'Camera'}</b>
          {state.camera_online ? 'On and watching.' : 'Not sending a picture.'}
        </div>
        <div className="mt-auto flex items-center justify-between gap-3 text-[13px] text-slate">
          <span className="flex items-center gap-2">
            <i className={cn('inline-block size-2.5 rounded-full', state.link?.connected ? 'bg-neem' : 'bg-haldi-fill')} />
            {state.link?.connected ? 'Connected to the SENTRIX Portal.' : state.link?.enabled ? 'No internet. Alerts go by SMS from this unit.' : 'Working on its own (no Portal link).'}
          </span>
          {s.canAdmin ? (
            <Link to="/admin" className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1 font-semibold text-graphite">
              <Settings className="size-4" aria-hidden />
              Device admin
            </Link>
          ) : null}
        </div>
      </aside>
    </div>
  );
}
