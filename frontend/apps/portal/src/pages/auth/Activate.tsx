import { ApiError } from '@sentrix/api';
import { Button, ErrorState, Field, Spinner, Strip, TextInput } from '@sentrix/ui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import { SESSION_KEY, api, homeFor, normaliseSession, setCsrf } from '../../lib/api';

/** Activation from the welcome letter: the QR code fills the code in. */
export function Activate() {
  const { code } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const link = useQuery({
    queryKey: ['activate', code],
    queryFn: () => api.get<{ username: string; password_min_length: number }>(`/api/v2/auth/activate/${encodeURIComponent(code!)}`),
    enabled: Boolean(code),
    retry: false,
  });
  const [username, setUsername] = useState('');
  const [typedCode, setTypedCode] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string>();
  const [busy, setBusy] = useState(false);
  const min = link.data?.password_min_length ?? 10;

  async function submit(e: FormEvent) {
    e.preventDefault();
    const errs: Record<string, string> = {};
    if (!code && !username.trim()) errs.username = 'Enter the username from your letter.';
    if (!code && !typedCode.trim()) errs.code = 'Enter the activation code from your letter.';
    if (password.length < min) errs.password = `Use at least ${min} characters.`;
    else if (password !== confirm) errs.password_confirm = 'The two passwords don’t match.';
    setErrors(errs);
    setMessage(undefined);
    if (Object.keys(errs).length) return;
    setBusy(true);
    try {
      const out = await (code
        ? api.post<any>(`/api/v2/auth/activate/${encodeURIComponent(code)}`, { password, password_confirm: confirm })
        : api.post<any>('/api/v2/auth/activate', { username: username.trim(), code: typedCode.trim(), password, password_confirm: confirm }));
      const s = normaliseSession(out.session ?? out);
      setCsrf(s);
      qc.setQueryData(SESSION_KEY, s);
      navigate(out.next || homeFor(s), { replace: true });
    } catch (err) {
      if (err instanceof ApiError) {
        setErrors(err.fields);
        setMessage(Object.keys(err.fields).length ? undefined : err.message);
      } else setMessage('Could not activate the account.');
    } finally {
      setBusy(false);
    }
  }

  if (code && link.isLoading) return <Spinner />;
  if (code && link.isError) {
    return (
      <div className="grid gap-4 pt-6">
        <ErrorState title="This activation link does not work" message="It may have been used already or be older than 72 hours. Ask SENTRIX for a new welcome letter, or sign in if you already chose a password." />
        <Link to="/login" className="font-semibold text-indigo underline">
          Sign in
        </Link>
      </div>
    );
  }
  return (
    <div className="grid gap-5 pt-6">
      <div>
        <h1 className="text-[28px] font-bold">{link.data ? `Welcome, ${link.data.username}` : 'Activate your account'}</h1>
        <p className="mt-1 text-[14px] text-slate">Choose a password to activate your SENTRIX account. Nobody at SENTRIX knows it or can see it.</p>
      </div>
      <form onSubmit={submit} noValidate className="grid gap-3 rounded-2xl bg-paper p-5 shadow-float">
        {message ? (
          <Strip tone="alarm" role="alert">
            {message}
          </Strip>
        ) : null}
        {code ? (
          <Strip tone="ok">The activation code from your letter is filled in. Username: {link.data?.username}</Strip>
        ) : (
          <>
            <Field label="Username" htmlFor="act-user" error={errors.username}>
              <TextInput id="act-user" autoCapitalize="none" autoComplete="username" value={username} invalid={Boolean(errors.username)} onChange={(e) => setUsername(e.target.value)} />
            </Field>
            <Field label="Activation code" htmlFor="act-code" help="12 letters on your welcome letter. Capitals and dashes don’t matter." error={errors.code}>
              <TextInput id="act-code" autoCapitalize="characters" value={typedCode} invalid={Boolean(errors.code)} onChange={(e) => setTypedCode(e.target.value)} />
            </Field>
          </>
        )}
        <Field label="Password" htmlFor="act-pw" help={`At least ${min} characters. A few words work well.`} error={errors.password}>
          <TextInput id="act-pw" type="password" autoComplete="new-password" value={password} invalid={Boolean(errors.password)} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label="Password again" htmlFor="act-pw2" error={errors.password_confirm}>
          <TextInput id="act-pw2" type="password" autoComplete="new-password" value={confirm} invalid={Boolean(errors.password_confirm)} onChange={(e) => setConfirm(e.target.value)} />
        </Field>
        <Button type="submit" variant="primary" busy={busy}>
          Activate my account
        </Button>
      </form>
      <p className="text-[13.5px] text-slate">
        Already activated? <Link to="/login" className="font-semibold text-indigo underline">Sign in</Link>.
      </p>
    </div>
  );
}
