import { createClient, type Client } from '@sentrix/api';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { DEFAULT_TZ } from '@sentrix/ui';

/** What the shells and guards need from GET /api/v2/session. The raw response
 *  stays available as `raw` for pages that need more. */
export interface Session {
  signedIn: boolean;
  role: 'owner' | 'member' | 'operator' | 'company_admin' | null;
  user: { id: string; username: string; name: string; totpEnabled: boolean; totpVerified: boolean; totpRequired: boolean } | null;
  household: { id: string; name: string; setupStep: number; setupDone: boolean; erasureDue: string | null } | null;
  timezone: string;
  testMode: boolean;
  deskNumber: string | null;
  csrf: string | null;
  loginCsrf: string | null;
  landing: string | null;
  steppedUp: boolean;
  passwordMin: number;
  raw: Record<string, any>;
}

/** Maps GET /api/v2/session (sentrix_portal/routes/api_v2/auth.py SessionOut). */
export function normaliseSession(raw: Record<string, any> | null | undefined): Session {
  const r = raw ?? {};
  const u = r.user ?? null;
  const h = r.household ?? null;
  const test = r.test_mode ?? {};
  return {
    signedIn: Boolean(r.signed_in),
    role: (u?.role ?? null) as Session['role'],
    user: u
      ? {
          id: String(u.id),
          username: u.username ?? '',
          name: u.display_name || u.username || '',
          totpEnabled: Boolean(u.totp?.enabled),
          totpVerified: Boolean(u.totp?.verified),
          totpRequired: Boolean(u.totp?.required),
        }
      : null,
    household: h
      ? {
          id: String(h.id),
          name: h.name ?? '',
          setupStep: Number(h.setup_step ?? 1),
          setupDone: Boolean(h.setup_done),
          erasureDue: h.erasure_due ?? null,
        }
      : null,
    timezone: h?.time_zone ?? DEFAULT_TZ,
    testMode: test.on !== false,
    deskNumber: test.desk_number ?? null,
    csrf: r.csrf_token ?? null,
    loginCsrf: r.login_csrf_token ?? null,
    landing: r.landing ?? null,
    steppedUp: Boolean(r.stepped_up),
    passwordMin: Number(r.password_min_length ?? 10),
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

/** Where a signed-in person lands. Staff must finish two-factor first. */
export function homeFor(s: Session): string {
  if ((s.role === 'operator' || s.role === 'company_admin') && s.user && !s.user.totpVerified) return '/account/2fa';
  if (s.role === 'operator') return '/desk';
  if (s.role === 'company_admin') return '/admin';
  if (s.household && !s.household.setupDone && s.role === 'owner') return '/setup';
  return '/home';
}
