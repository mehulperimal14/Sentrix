import { ApiError } from '@sentrix/api';
import { Button, Field, TextInput, Wordmark } from '@sentrix/ui';
import { useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { api, normaliseSession, setCsrf } from '../lib/api';

export function Login() {
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const qc = useQueryClient();

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!password) {
      setError('Enter the device password.');
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      await api.post('/api/login', { password });
      const s = normaliseSession(await api.get('/api/session'));
      setCsrf(s.csrf);
      qc.setQueryData(['session'], s);
      const next = params.get('next');
      navigate(next && next.startsWith('/') && !next.startsWith('//') ? next : '/', { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? (err.code === 'rate_limited' ? 'Too many tries. Wait a few minutes.' : err.status === 401 || err.status === 403 ? 'That password is not right.' : err.message) : 'Could not sign in.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="grid min-h-dvh place-items-center bg-chalk p-4">
      <form onSubmit={submit} noValidate className="grid w-full max-w-sm gap-4 rounded-2xl bg-paper p-6 shadow-float">
        <Wordmark />
        <div>
          <h1 className="text-[24px] font-bold">Sign in to this device</h1>
          <p className="mt-1 text-[13.5px] text-slate">The device password is on the setup sheet the installer left. Owners use the SENTRIX app on their phone instead.</p>
        </div>
        <Field label="Device password" htmlFor="pw" error={error}>
          <TextInput id="pw" type="password" autoComplete="current-password" autoFocus value={password} invalid={Boolean(error)} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Button type="submit" variant="primary" busy={busy}>
          Sign in
        </Button>
      </form>
    </main>
  );
}
