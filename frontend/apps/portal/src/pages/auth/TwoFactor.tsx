import { ApiError } from '@sentrix/api';
import { Button, Field, QrCode, Spinner, Strip, TextInput } from '@sentrix/ui';
import { useQuery } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router';
import { api, homeFor, useRefreshSession } from '../../lib/api';

interface TwoFactorOut {
  required: boolean;
  enabled: boolean;
  verified: boolean;
  stepped_up: boolean;
  recovery_codes_left: number;
  setup: { key: string; uri: string } | null;
}

/** Staff two-factor: set up an authenticator app, then enter its code each sign-in. */
export function TwoFactor() {
  const navigate = useNavigate();
  const refresh = useRefreshSession();
  const q = useQuery({ queryKey: ['2fa'], queryFn: () => api.get<TwoFactorOut>('/api/v2/account/2fa') });
  const [code, setCode] = useState('');
  const [error, setError] = useState<string>();
  const [codes, setCodes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  if (q.isLoading) return <Spinner />;
  const d = q.data;
  if (!d) return <Strip tone="alarm">Two-factor settings are not available.</Strip>;
  const settingUp = Boolean(d.setup) && !d.enabled;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!/^\d{6}$/.test(code.trim()) && !/^[A-Z0-9-]{8,}$/i.test(code.trim())) {
      setError('Enter the 6-digit code from your authenticator app, or a recovery code.');
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      if (settingUp) {
        const out = await api.post<{ codes?: string[]; recovery_codes?: string[] }>('/api/v2/account/2fa/setup', { code: code.trim() });
        setCodes(out.codes ?? out.recovery_codes ?? []);
      } else {
        await api.post('/api/v2/account/2fa/verify', { code: code.trim() });
        const s = await refresh();
        navigate(homeFor(s), { replace: true });
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'That code did not work.');
    } finally {
      setBusy(false);
    }
  }

  if (codes) {
    return (
      <div className="grid max-w-md gap-4">
        <h1 className="text-[24px] font-bold">Save your recovery codes</h1>
        <p className="text-[14px] text-slate">Each works once if you lose your phone. They are shown only now.</p>
        <pre className="rounded-xl border border-line bg-paper p-3 font-mono text-[14px] leading-7">{codes.join('\n')}</pre>
        <Button
          variant="primary"
          onClick={async () => {
            const s = await refresh();
            navigate(homeFor(s), { replace: true });
          }}
        >
          I have saved them
        </Button>
      </div>
    );
  }
  return (
    <div className="grid max-w-md gap-4">
      <h1 className="text-[24px] font-bold">{settingUp ? 'Set up two-factor sign-in' : 'Enter your authenticator code'}</h1>
      {settingUp && d.setup ? (
        <div className="grid gap-3 rounded-2xl bg-paper p-4">
          <p className="text-[14px] text-slate">Scan this with an authenticator app, then type the 6-digit code it shows.</p>
          <div className="justify-self-center">
            <QrCode value={d.setup.uri} size={168} label="Authenticator set-up code" />
          </div>
          <p className="text-[12.5px] text-slate">
            Or type this key: <span className="select-all font-mono">{d.setup.key}</span>
          </p>
        </div>
      ) : null}
      <form onSubmit={submit} noValidate className="grid gap-3 rounded-2xl bg-paper p-4">
        <Field label="Code" htmlFor="totp" error={error} help={settingUp ? undefined : `You have ${d.recovery_codes_left} recovery codes left.`}>
          <TextInput id="totp" inputMode="numeric" autoComplete="one-time-code" value={code} invalid={Boolean(error)} onChange={(e) => setCode(e.target.value)} />
        </Field>
        <Button type="submit" variant="primary" busy={busy}>
          {settingUp ? 'Turn on two-factor' : 'Continue'}
        </Button>
      </form>
    </div>
  );
}
