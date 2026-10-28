import { ApiError } from '@sentrix/api';
import { Button, EmptyState, ErrorState, PinPad, Segmented, Sheet, Spinner, StatusCard, Strip, formatDayTime, formatTime, useToast } from '@sentrix/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { ActivityRow } from '../../features/activity/ActivityRow';
import { headline } from '../../features/alert/AlertScreen';
import { deviceApi, useSession } from '../../lib/api';
import { useHome, type RawDevice, type RawRequest } from '../../lib/data';
import { useDeviceActivity } from '../../lib/activity';
import { SetupChecklist } from './SetupChecklist';

/** Home answers "is my home OK?" first (Experience Plan, journey "Everyday"). */
export function Home() {
  const { data: s } = useSession();
  const home = useHome();
  if (home.isLoading) return <Spinner />;
  if (home.isError || !home.data) return <div className="p-4"><ErrorState message="Could not load your home." onRetry={() => home.refetch()} /></div>;
  const h = home.data;
  const tz = s?.timezone ?? 'Asia/Kolkata';
  return (
    <div className="grid gap-4 px-4 py-4 safe-x md:px-6">
      {s?.household?.erasureDue ? (
        <Strip tone="alarm">
          This account is scheduled for erasure on {formatDayTime(s.household.erasureDue, tz)}. <Link to="/settings/privacy" className="font-semibold underline">Keep my account</Link>
        </Strip>
      ) : null}
      <h1 className="text-[22px] font-bold">{h.name ? `${h.name}` : 'Your home'}</h1>
      {h.devices.length === 0 ? (
        <EmptyState title="No device yet" action={<Link to="/setup/device" className="font-semibold text-indigo underline">Add the device that came in the box</Link>}>
          Nothing is watching your home until a device is added.
        </EmptyState>
      ) : (
        h.devices.map((d) => <DeviceHome key={d.id} device={d} requests={h.requests} tz={tz} />)
      )}
      <SetupChecklist compact />
    </div>
  );
}

function DeviceHome({ device: d, requests, tz }: { device: RawDevice; requests: RawRequest[]; tz: string }) {
  const toast = useToast();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { data: s } = useSession();
  const [pin, setPin] = useState(false);
  const [pinError, setPinError] = useState<string>();
  const dapi = deviceApi(d.id);
  const live = useQuery({
    queryKey: ['device-state', d.id],
    queryFn: () => dapi.get<any>('/api/state'),
    enabled: d.online && d.claimed,
    refetchInterval: 5000,
    retry: false,
  });
  const activity = useDeviceActivity(d, 3);
  const mode = useMutation({
    mutationFn: (body: { armed: boolean; pin?: string }) => dapi.post('/api/mode', body),
    onSuccess: (_r, v) => {
      setPin(false);
      setPinError(undefined);
      toast(v.armed ? 'Away mode on. Someone SENTRIX doesn’t recognise shows after 3 seconds.' : 'Home mode on.');
      qc.invalidateQueries({ queryKey: ['home'] });
      qc.invalidateQueries({ queryKey: ['device-state', d.id] });
    },
    onError: (e, v) => {
      const msg = e instanceof ApiError ? e.message : 'That did not work.';
      if (!v.armed) setPinError(msg);
      else toast(msg);
    },
  });

  const open = requests.find((r) => r.device_id === d.id && ['PENDING', 'DRAFTED', 'APPROVED', 'DISPATCHED', 'ACKNOWLEDGED', 'FAILED'].includes(r.state));
  const state = live.data;
  const level = state?.alarm?.level ?? state?.level ?? 1;
  const unusual = level === 2;
  const armed = state?.mode?.armed ?? d.armed;

  let status;
  if (!d.claimed) {
    status = (
      <>
        <StatusCard tone="off" title="Not added yet">Nothing is watching your home until the device is added.</StatusCard>
        <Button variant="primary" onClick={() => navigate('/setup/device')}>Add the device</Button>
      </>
    );
  } else if (open) {
    const waiting = open.state === 'PENDING' || open.state === 'DRAFTED';
    status = (
      <>
        <StatusCard tone="alarm" title={waiting ? headline({ incidentType: open.incident_type ?? 'threat', evidence: open.evidence_state, room: d.room }) : open.state === 'FAILED' ? 'Help request not delivered' : `Help requested at ${formatTime(open.updated_at ?? open.created_at ?? Date.now(), tz)}`}>
          {waiting ? 'Needs your answer.' : open.label}
        </StatusCard>
        <Button variant={waiting ? 'alarm' : 'secondary'} onClick={() => navigate(`/alert/${open.id}`)}>
          {waiting ? 'Answer the alert' : 'See who was told'}
        </Button>
      </>
    );
  } else if (!d.online) {
    status = (
      <>
        <StatusCard tone="off" title={`${d.name} is offline`}>
          {d.last_seen ? `Since ${formatDayTime(d.last_seen, tz)}. ` : ''}It keeps watching and can sound the siren; alerts reach you by SMS from the unit.
        </StatusCard>
        <details className="rounded-xl bg-haldi-wash px-3 py-2 text-[13px]">
          <summary className="cursor-pointer font-semibold">What to check</summary>
          Is the device plugged in? Is your Wi-Fi working? If its amber light stays on, it can’t reach the internet.
        </details>
      </>
    );
  } else if (unusual) {
    status = (
      <StatusCard tone="warn" title={state?.alarm?.incident_type === 'intrusion' || state?.incident_type === 'intrusion' ? 'Someone we don’t recognise' : 'Something unusual'}>
        In the {d.room ?? 'room'} now. Only you can see this; nobody else was told.
      </StatusCard>
    );
  } else {
    status = (
      <StatusCard tone="ok" title="All clear">
        {d.room ?? 'The'} camera is on.{armed ? ' Away mode.' : ' Nothing unusual right now.'}
      </StatusCard>
    );
  }

  return (
    <section aria-label={d.name} className="grid gap-3">
      {status}
      {d.claimed ? (
        <Segmented
          label="Mode"
          value={armed ? 'away' : 'home'}
          disabled={!d.online || mode.isPending || s?.role !== 'owner' && s?.role !== 'member'}
          onChange={(v) => (v === 'away' ? mode.mutate({ armed: true }) : setPin(true))}
          options={[
            { value: 'home', label: 'Home' },
            { value: 'away', label: 'Away' },
          ]}
        />
      ) : null}
      {d.claimed && d.online ? (
        <Link to={`/live/${d.id}`} className="relative block overflow-hidden rounded-xl bg-hatch" aria-label={`Open the live picture of the ${d.room ?? 'room'}`}>
          <img src={`/d/${d.id}/video`} alt="" className="aspect-[16/7] w-full object-cover" />
          <span className="absolute bottom-2 left-2 rounded-md bg-black/55 px-2 py-0.5 text-[11px] font-semibold text-white">Live picture. Tap to open</span>
        </Link>
      ) : null}
      {d.claimed ? (
        <div className="grid gap-2">
          <h2 className="font-sans text-[13px] font-bold text-slate">Today</h2>
          {activity.items.length ? activity.items.map((a) => <ActivityRow key={a.id} item={a} timezone={tz} onOpen={() => navigate(`/activity?item=${encodeURIComponent(a.id)}`)} />) : <p className="text-[13px] text-slate">Nothing yet today.</p>}
          <Link to="/activity" className="text-[13px] font-semibold text-indigo underline">
            See all activity
          </Link>
        </div>
      ) : null}
      <Sheet open={pin} onClose={() => setPin(false)}>
        <PinPad title="Enter your SOS PIN to switch to Home" error={pinError} busy={mode.isPending} onCancel={() => setPin(false)} onSubmit={(p) => mode.mutate({ armed: false, pin: p })} />
      </Sheet>
    </section>
  );
}
