import { ApiError } from '@sentrix/api';
import { Button, Field, Strip, TextInput } from '@sentrix/ui';
import { useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { Link, Navigate, useNavigate, useSearchParams } from 'react-router';
import { SESSION_KEY, api, homeFor, normaliseSession, setCsrf, useSession } from '../../lib/api';

export function Login() {
  const session = useSession();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string>();
  const [busy, setBusy] = useState(false);
  const next = params.get('next');

  if (session.data?.signedIn) return <Navigate to={next && next.startsWith('/') && !next.startsWith('//') ? next : homeFor(session.data)} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    const errs: Record<string, string> = {};
    if (!username.trim()) errs.username = 'Enter your username.';
    if (!password) errs.password = 'Enter your password.';
    setErrors(errs);
    setMessage(undefined);
    if (Object.keys(errs).length) return;
    setBusy(true);
    try {
      const out = await api.post<{ session: Record<string, any>; next: string }>('/api/v2/auth/login', { username: username.trim(), password, next: next ?? undefined });
      const s = normaliseSession(out.session);
      setCsrf(s);
      qc.setQueryData(SESSION_KEY, s);
      navigate(homeFor(s) === '/account/2fa' ? '/account/2fa' : out.next || homeFor(s), { replace: true });
    } catch (err) {
      if (err instanceof ApiError) {
        setErrors(err.fields);
        setMessage(err.code === 'rate_limited' && err.retryAfter ? `Too many tries. Wait ${Math.ceil(err.retryAfter)} seconds and try again.` : err.message);
      } else setMessage('Could not sign in.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-5 pt-6">
      <div>
        <h1 className="text-[28px] font-bold">Sign in</h1>
        <p className="mt-1 text-[14px] text-slate">Your home, your alerts and your people.</p>
      </div>
      <form onSubmit={submit} noValidate className="grid gap-3 rounded-2xl bg-paper p-5 shadow-float">
        {message ? (
          <Strip tone="alarm" role="alert">
            {message}
          </Strip>
        ) : null}
        <Field label="Username" htmlFor="username" error={errors.username}>
          <TextInput id="username" autoComplete="username" autoCapitalize="none" value={username} invalid={Boolean(errors.username)} onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <Field label="Password" htmlFor="password" error={errors.password}>
          <TextInput id="password" type="password" autoComplete="current-password" value={password} invalid={Boolean(errors.password)} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Button type="submit" variant="primary" busy={busy}>
          Sign in
        </Button>
      </form>
      <p className="text-[13.5px] text-slate">
        New to SENTRIX? Use the activation code in your welcome letter: <Link to="/activate" className="font-semibold text-indigo underline">activate your account</Link>. Forgot your password? Ask SENTRIX to send a reset link to your e-mail.
      </p>
    </div>
  );
}
