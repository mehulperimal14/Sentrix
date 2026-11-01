import { createClient, type Client } from '@sentrix/api';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import type { LiveState } from './state';

/** The device console's session (GET /api/session). Fields the screens use;
 *  `normaliseSession` tolerates extra or missing keys. */
export interface DeviceSession {
  signedIn: boolean;
  role: string | null;
  csrf?: string;
  tunnelled: boolean;
  testMode: boolean;
  call: { label: string; number: string | null; enabled: boolean; note?: string };
  device: { serial: string | null; name: string | null; room: string | null; claimed: boolean };
  setupMode: boolean;
  timezone: string;
  canAdmin: boolean;
}

type Raw = Record<string, any>;

/** Maps GET /api/session (sentrix/web/api_models.py SessionInfo). */
export function normaliseSession(raw: Raw | null | undefined): DeviceSession {
  const r = raw ?? {};
  const device = r.device ?? {};
  const call = r.call ?? {};
  const role = r.role ?? null;
  const testMode = r.test_mode !== false;
  return {
    signedIn: Boolean(r.signed_in),
    role,
    csrf: r.csrf_token ?? undefined,
    tunnelled: r.via === 'portal',
    testMode,
    call: {
      // Test mode never says "112": the Desk if it has a number, else a plain word.
      label: call.desk ? 'Call the SENTRIX Desk' : testMode ? 'Calling is off in this demo' : 'Call 112 now',
      number: call.enabled ? call.number ?? (call.href ? String(call.href).replace(/^tel:/, '') : null) : null,
      enabled: Boolean(call.enabled),
      note: call.note || undefined,
    },
    device: {
      serial: device.serial ?? null,
      name: device.name ?? null,
      room: device.room ?? null,
      claimed: Boolean(r.claimed ?? r.link?.claimed),
    },
    setupMode: Boolean(r.setup_mode),
    timezone: r.time_zone ?? 'Asia/Kolkata',
    canAdmin: Boolean(r.lan_admin) || role === 'device_admin',
  };
}

let csrfToken: string | undefined;
export function setCsrf(token: string | undefined) {
  csrfToken = token;
}

export const api: Client = createClient({
  csrf: () => csrfToken,
  onUnauthorized: () => {
    if (!location.pathname.startsWith('/login') && !location.pathname.startsWith('/setup')) {
      location.assign(`/login?next=${encodeURIComponent(location.pathname)}`);
    }
  },
});

export function useSession() {
  return useQuery({
    queryKey: ['session'],
    queryFn: async () => {
      const s = normaliseSession(await api.get<Raw>('/api/session'));
      setCsrf(s.csrf);
      return s;
    },
    staleTime: 60_000,
  });
}

/** The live state: one GET for the first paint, then the /ws/threat socket.
 *  `stale` turns true when nothing has arrived for 10 s (the screen then says
 *  "Not connected" instead of showing old values as if they were current). */
export function useLiveState(enabled = true) {
  const [state, setState] = useState<LiveState | null>(null);
  const [lastAt, setLastAt] = useState<number>(0);
  const [now, setNow] = useState(() => Date.now());
  const retry = useRef(0);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    api
      .get<LiveState>('/api/state')
      .then((s) => {
        if (alive) {
          setState(s);
          setLastAt(Date.now());
        }
      })
      .catch(() => undefined);
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      ws = new WebSocket(`${proto}//${location.host}/ws/threat`);
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data as string);
          if (data && typeof data === 'object' && 'level' in data) {
            setState(data as LiveState);
            setLastAt(Date.now());
            retry.current = 0;
          }
        } catch {
          /* ignore a malformed frame */
        }
      };
      ws.onclose = () => {
        if (!alive) return;
        retry.current = Math.min(retry.current + 1, 6);
        timer = setTimeout(connect, 500 * 2 ** retry.current);
      };
    };
    connect();
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      alive = false;
      clearInterval(tick);
      if (timer) clearTimeout(timer);
      ws?.close();
    };
  }, [enabled]);

  const stale = lastAt === 0 ? false : now - lastAt > 10_000;
  return { state, stale, lastAt };
}
