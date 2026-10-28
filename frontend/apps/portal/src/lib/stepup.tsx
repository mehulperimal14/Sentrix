import { ApiError } from '@sentrix/api';
import { Button, Field, Sheet, TextInput } from '@sentrix/ui';
import { createContext, useCallback, useContext, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { api, useRefreshSession } from './api';

type Run = <T>(action: () => Promise<T>) => Promise<T>;
const Ctx = createContext<Run>((a) => a());

/** Sensitive changes (PINs, export, erasure, password) answer
 *  `step_up_required` until the password is typed again; `useStepUp()(fn)`
 *  asks for it once and retries `fn`. */
export function StepUpProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const pending = useRef<{ resolve: () => void; reject: (e: unknown) => void } | null>(null);
  const refresh = useRefreshSession();

  const run: Run = useCallback(async (action) => {
    try {
      return await action();
    } catch (err) {
      if (!(err instanceof ApiError) || err.code !== 'step_up_required') throw err;
      await new Promise<void>((resolve, reject) => {
        pending.current = { resolve, reject };
        setPassword('');
        setError(undefined);
        setOpen(true);
      });
      return action();
    }
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      await api.post('/api/v2/auth/step-up', { password });
      await refresh();
      setOpen(false);
      pending.current?.resolve();
      pending.current = null;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'That did not work.');
    } finally {
      setBusy(false);
    }
  }
  function cancel() {
    setOpen(false);
    pending.current?.reject(new ApiError(403, { error: { code: 'step_up_cancelled', message: 'Not changed.' } }, 'Not changed.'));
    pending.current = null;
  }
  return (
    <Ctx.Provider value={run}>
      {children}
      <Sheet open={open} onClose={cancel} title="Type your password again">
        <form onSubmit={submit} noValidate className="grid gap-3">
          <p className="text-[13.5px] text-slate">This change needs your password. You won’t be asked again for a few minutes.</p>
          <Field label="Password" htmlFor="stepup-pw" error={error}>
            <TextInput id="stepup-pw" type="password" autoComplete="current-password" autoFocus value={password} invalid={Boolean(error)} onChange={(e) => setPassword(e.target.value)} />
          </Field>
          <Button type="submit" variant="primary" busy={busy}>
            Continue
          </Button>
          <Button variant="quiet" onClick={cancel}>
            Cancel
          </Button>
        </form>
      </Sheet>
    </Ctx.Provider>
  );
}

export function useStepUp(): Run {
  return useContext(Ctx);
}
