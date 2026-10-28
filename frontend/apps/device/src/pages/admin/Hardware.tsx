import { ApiError } from '@sentrix/api';
import { Button, Check, Spinner, Strip, formatDuration, useToast } from '@sentrix/ui';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../../lib/api';
import { KV, Panel, yesNo } from './ui';

/** The unit itself: telemetry, inputs and outputs, the siren test. */
export default function Hardware() {
  const toast = useToast();
  const [atUnit, setAtUnit] = useState(false);
  const node = useQuery({ queryKey: ['node'], queryFn: () => api.get<any>('/api/node'), refetchInterval: 5000 });
  const health = useQuery({ queryKey: ['health'], queryFn: () => api.get<any>('/api/health'), refetchInterval: 10_000 });
  const siren = useMutation({
    mutationFn: () => api.post('/api/node/siren-test', {}),
    onSuccess: () => toast('The siren sounded for 1 second.'),
    onError: (e) => toast(e instanceof ApiError ? (e.code === 'rate_limited' ? 'One test a minute: wait and try again.' : e.message) : 'The siren test failed.'),
  });
  if (node.isLoading) return <Spinner />;
  if (node.isError) return <Strip tone="warn">The hardware status is not available.</Strip>;
  const n = node.data ?? {};
  const h = health.data ?? {};
  const leds = n.gpio?.leds ?? n.leds ?? {};
  const mic = n.microphone ?? {};
  return (
    <div className="grid gap-4">
      <h1 className="text-[24px] font-bold">Hardware</h1>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        <Panel title="This unit">
          <KV
            rows={[
              ['Profile', n.profile ?? '—'],
              ['CPU temperature', n.cpu_temp_c != null ? `${Number(n.cpu_temp_c).toFixed(1)} °C` : '—'],
              ['Throttled', yesNo(n.throttled)],
              ['Under-voltage now', yesNo(n.under_voltage)],
              ['5 V rail', n.ext5v_v != null ? `${Number(n.ext5v_v).toFixed(2)} V` : '—'],
              ['Mains power', yesNo(n.mains)],
              ['Uptime', n.uptime_s != null ? formatDuration(n.uptime_s) : '—'],
            ]}
          />
        </Panel>
        <Panel title="Camera and microphone">
          <KV
            rows={[
              ['Camera', n.camera?.online ? `on (${n.camera?.name ?? 'camera'})` : 'no signal'],
              ['Microphone', mic?.device ? `${mic.device}${mic.native_rate ? `, ${mic.native_rate} Hz` : ''}` : 'not connected'],
              ['Sound model', h.audio ?? '—'],
            ]}
          />
        </Panel>
        <Panel title="Lights, siren and sensors">
          <KV
            rows={[
              ['Green light', leds.green ?? '—'],
              ['Amber light', leds.amber ?? '—'],
              ['Red light', leds.red ?? '—'],
              ['Siren', n.siren ? `${n.siren.backend ?? 'siren'}, ${n.siren_armed ? 'armed' : 'disarmed'}, ${n.siren.active ? 'sounding' : 'off'}` : '—'],
              ['Motion (PIR)', yesNo(n.pir)],
              ['Door', yesNo(n.door)],
            ]}
          />
          <Check id="at-unit" checked={atUnit} onChange={setAtUnit} label="I am at the unit and expect a 1-second siren" />
          <Button variant="dark" disabled={!atUnit} busy={siren.isPending} onClick={() => siren.mutate()}>
            Test the siren (1 s)
          </Button>
        </Panel>
        <Panel title="Services" className="md:col-span-2 xl:col-span-3">
          <KV
            rows={Object.entries(h)
              .filter(([, v]) => typeof v !== 'object' || v === null)
              .map(([k, v]) => [k.replace(/_/g, ' '), yesNo(v)] as [string, string])}
          />
        </Panel>
      </div>
    </div>
  );
}
