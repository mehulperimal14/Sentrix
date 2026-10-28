import { ApiError } from '@sentrix/api';
import { Button, Field, Spinner, Strip, TextInput, Wordmark } from '@sentrix/ui';
import { useQuery } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { api } from '../lib/api';

interface Network {
  ssid: string;
  signal: number | null;
  security: string | null;
}

/** Wi-Fi setup mode, on the device's own network, with the setup code on the label. */
export default function WifiSetup() {
  const info = useQuery({ queryKey: ['setup-wifi'], queryFn: () => api.get<any>('/api/setup/wifi'), retry: false });
  const [code, setCode] = useState('');
  const [networks, setNetworks] = useState<Network[] | null>(null);
  const [ssid, setSsid] = useState('');
  const [psk, setPsk] = useState('');
  const [msg, setMsg] = useState<{ tone: 'ok' | 'warn' | 'info'; text: string } | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<'scan' | 'connect' | null>(null);

  async function scan() {
    setErrors({});
    if (!code.trim()) {
      setErrors({ code: 'Type the setup code from the label first.' });
      return;
    }
    setBusy('scan');
    setMsg({ tone: 'info', text: 'Looking for networks…' });
    try {
      const r = await api.post<any>('/api/setup/wifi/scan', { code });
      setNetworks(r.networks ?? []);
      setMsg(r.networks?.length ? null : { tone: 'warn', text: 'No networks found. Move the unit closer to the router.' });
    } catch (e) {
      setMsg({ tone: 'warn', text: e instanceof ApiError ? e.message : 'Could not look for networks.' });
      if (e instanceof ApiError) setErrors(e.fields);
    } finally {
      setBusy(null);
    }
  }

  async function connect(e: FormEvent) {
    e.preventDefault();
    setErrors({});
    if (!ssid.trim()) {
      setErrors({ ssid: 'Choose a network first.' });
      return;
    }
    setBusy('connect');
    setMsg({ tone: 'info', text: 'Connecting…' });
    try {
      const r = await api.post<any>('/api/setup/wifi/connect', { code, ssid, psk: psk || undefined });
      setMsg({ tone: 'ok', text: `Connected${r.status?.ssid ? ` to ${r.status.ssid}` : ''}. The unit leaves setup mode when you press its setup button again.` });
    } catch (err) {
      setMsg({ tone: 'warn', text: err instanceof ApiError ? err.message : 'Could not connect.' });
      if (err instanceof ApiError) setErrors(err.fields);
    } finally {
      setBusy(null);
    }
  }

  if (info.isLoading) return <Spinner />;
  if (info.isError) {
    return (
      <main className="grid min-h-dvh place-items-center p-4">
        <Strip tone="info">Wi-Fi setup is not open. Press the setup button on the unit to start it.</Strip>
      </main>
    );
  }
  const d = info.data ?? {};
  return (
    <main className="grid min-h-dvh place-items-center bg-chalk p-4">
      <div className="grid w-full max-w-md gap-4 rounded-2xl bg-paper p-6 shadow-float">
        <Wordmark />
        <h1 className="text-[24px] font-bold">Connect the device to Wi-Fi</h1>
        {!d.network_available ? (
          <Strip tone="warn">Wi-Fi control is not available on this unit. Connect it with a network cable instead.</Strip>
        ) : (
          <form onSubmit={connect} noValidate className="grid gap-3">
            <Field label="Setup code" htmlFor="code" help="Printed on the device label, like QZPJ-ZNWP." error={errors.code}>
              <TextInput id="code" value={code} onChange={(e) => setCode(e.target.value)} autoCapitalize="characters" placeholder="XXXX-XXXX" invalid={Boolean(errors.code)} />
            </Field>
            <Button busy={busy === 'scan'} onClick={scan}>
              Find networks
            </Button>
            {networks?.length ? (
              <div role="radiogroup" aria-label="Networks" className="grid gap-1.5">
                {networks.map((n) => (
                  <label key={n.ssid} className="flex items-center justify-between gap-2 rounded-xl border-[1.5px] border-line px-3 py-2 text-[14px]">
                    <span className="flex items-center gap-2">
                      <input type="radio" name="ssid" checked={ssid === n.ssid} onChange={() => setSsid(n.ssid)} />
                      {n.ssid}
                    </span>
                    <small className="text-slate">
                      {n.signal != null ? `${n.signal}%` : ''} {n.security ?? ''}
                    </small>
                  </label>
                ))}
              </div>
            ) : null}
            <Field label="Network name" htmlFor="ssid" error={errors.ssid}>
              <TextInput id="ssid" value={ssid} onChange={(e) => setSsid(e.target.value)} invalid={Boolean(errors.ssid)} />
            </Field>
            <Field label="Wi-Fi password" htmlFor="psk" error={errors.psk} help="8 to 63 characters.">
              <TextInput id="psk" type="password" autoComplete="off" value={psk} onChange={(e) => setPsk(e.target.value)} invalid={Boolean(errors.psk)} />
            </Field>
            <Button type="submit" variant="primary" busy={busy === 'connect'}>
              Connect
            </Button>
          </form>
        )}
        {msg ? (
          <Strip tone={msg.tone} role="status">
            {msg.text}
          </Strip>
        ) : null}
      </div>
    </main>
  );
}
