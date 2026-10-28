import { createClient, type Client } from '@sentrix/api';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { DEFAULT_TZ } from '@sentrix/ui';

/** What the shells and guards need from GET /api/v2/session. The raw response
 *  stays available as `raw` for pages that need more. */
export interface Session {
  signedIn: boolean;
  role: 'owner' | 'member' | 'operator' | 'company_admin' | null;
  user: { id: string; username: string; name: string; totpEnabled: boolean; totpVerified: boolean } | null;
  household: { id: string; name: string; setupStep: number; setupDone: boolean } | null;
  timezone: string;
  testMode: boolean;
  deskNumber: string | null;
  csrf: string | null;
  loginCsrf: string | null;
  raw: Record<string, any>;
}

export function normaliseSession(raw: Record<string, any> | null | undefined): Session {
  const r = raw ?? {};
  const u = r.user ?? null;
  const h = r.household ?? null;
  const test = r.test_mode ?? r.testing ?? {};
  const testOn = typeof test === 'boolean' ? test : Boolean(test.on ?? test.enabled ?? test.test_mode ?? true);
  return {
    signedIn: Boolean(r.signed_in ?? u),
    role: (u?.role ?? r.role ?? null) as Session['role'],
    user: u
      ? {
          id: String(u.id ?? ''),
          username: u.username ?? '',
          name: u.display_name ?? u.name ?? u.username ?? '',
          totpEnabled: Boolean(u.totp_enabled ?? u.totp?.enabled),
          totpVerified: Boolean(u.totp_verified ?? u.totp?.verified),
        }
      : null,
    household: h
      ? {
          id: String(h.id ?? ''),
          name: h.name ?? '',
          setupStep: Number(h.setup_step ?? h.onboarding_step ?? 1),
          setupDone: Boolean(h.setup_done ?? h.onboarding_done ?? h.setup_complete),
        }
      : null,
    timezone: h?.timezone ?? h?.time_zone ?? r.timezone ?? DEFAULT_TZ,
    testMode: testOn,
    deskNumber: (typeof test === 'object' ? test.desk_number : null) ?? r.desk_number ?? null,
    csrf: r.csrf_token ?? null,
    loginCsrf: r.login_csrf ?? r.login_csrf_token ?? null,
    raw: r,
  };
}

let csrfToken: string | null = null;
export function setCsrf(s: Session | null) {
  csrfToken = s ? s.csrf ?? s.loginCsrf : null;
}

export const api: Client = createClient({
  csrf: () => csrfToken ?? undefined,
});

/** A client for one device's own API through the Portal tunnel. */
export function deviceApi(deviceId: string): Client {
  return createClient({ base: `/d/${encodeURIComponent(deviceId)}`, csrf: () => csrfToken ?? undefined });
}

export const SESSION_KEY = ['session'] as const;

export async function fetchSession(): Promise<Session> {
  const s = normaliseSession(await api.get('/api/v2/session'));
  setCsrf(s);
  return s;
}

export function useSession() {
  return useQuery({ queryKey: SESSION_KEY, queryFn: fetchSession, staleTime: 60_000 });
}

export function useRefreshSession() {
  const qc = useQueryClient();
  return async () => {
    const s = await fetchSession();
    qc.setQueryData(SESSION_KEY, s);
    return s;
  };
}

/** Where a signed-in person lands. */
export function homeFor(s: Session): string {
  if (s.role === 'operator') return '/desk';
  if (s.role === 'company_admin') return '/admin';
  if (s.household && !s.household.setupDone && s.role === 'owner') return '/setup';
  return '/home';
}
