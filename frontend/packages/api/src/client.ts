/** Fetch client shared by both apps: JSON in and out, the CSRF header on
 *  state-changing calls, and one error type that carries the API's error model
 *  (docs/9_FRONTEND.md section 4). */

export interface ApiErrorBody {
  error?: { code?: string; message?: string; fields?: Record<string, string>; retry_after?: number };
  /** The device console's older shape: {"detail": message, "code": code}. */
  detail?: unknown;
  code?: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly fields: Record<string, string>;
  readonly retryAfter?: number;

  constructor(status: number, body: ApiErrorBody | null, fallback: string, retryAfterHeader?: string | null) {
    const e = body?.error;
    const legacyCode = typeof body?.code === 'string' ? body.code : undefined;
    const detail = typeof body?.detail === 'string' ? body.detail : undefined;
    const code = e?.code || legacyCode || codeFor(status);
    super(e?.message || friendly(status, code) || detail || fallback);
    this.status = status;
    this.code = code;
    this.fields = e?.fields || {};
    const header = retryAfterHeader ? Number(retryAfterHeader) : undefined;
    this.retryAfter = e?.retry_after ?? (Number.isFinite(header) ? header : undefined);
  }
}

function codeFor(status: number): string {
  if (status === 401) return 'not_signed_in';
  if (status === 403) return 'forbidden';
  if (status === 404) return 'not_found';
  if (status === 409) return 'conflict';
  if (status === 422) return 'validation';
  if (status === 429) return 'rate_limited';
  if (status >= 500) return 'server';
  return 'error';
}

function friendly(status: number, code?: string): string | undefined {
  switch (code) {
    case 'not_signed_in':
      return 'Please sign in again.';
    case 'csrf':
      return 'This page is out of date. Reload it and try again.';
    case 'step_up_required':
      return 'Enter your password again to continue.';
    case 'device_offline':
      return 'The device is offline. Try again when it is back.';
    case 'rate_limited':
      return 'Too many tries. Wait a moment and try again.';
    case 'pin_locked':
      return 'Too many wrong PINs. Try again in 10 minutes; asking for help still works.';
    case 'pin_invalid':
      return 'That PIN is not right.';
    case 'forbidden_role':
      return 'Your account can’t do this.';
    case 'test_mode_only':
      return 'This works only in test mode.';
  }
  if (status === 404) return 'Not found.';
  if (status >= 500) return 'The server had a problem. Try again in a moment.';
  return undefined;
}

export interface ClientOptions {
  /** Prefix for every path, e.g. "" or "/d/<device id>" for the tunnel. */
  base?: string;
  /** Returns the CSRF token to send on state-changing calls. */
  csrf?: () => string | undefined;
  /** Header name for the token (both apps use X-CSRF-Token). */
  csrfHeader?: string;
  /** Called on 401 so the app can go to sign-in. */
  onUnauthorized?: () => void;
}

export type Json = Record<string, unknown> | unknown[] | string | number | boolean | null;

export function createClient(opts: ClientOptions = {}) {
  const base = opts.base ?? '';
  const header = opts.csrfHeader ?? 'X-CSRF-Token';

  async function request<T>(method: string, path: string, body?: Json | FormData, init?: RequestInit): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json' };
    let payload: BodyInit | undefined;
    if (body instanceof FormData) payload = body;
    else if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      payload = JSON.stringify(body);
    }
    if (method !== 'GET' && method !== 'HEAD') {
      const token = opts.csrf?.();
      if (token) headers[header] = token;
    }
    let res: Response;
    try {
      res = await fetch(base + path, { method, headers, body: payload, credentials: 'same-origin', ...init });
    } catch {
      throw new ApiError(0, { error: { code: 'network', message: 'No connection. Check the internet and try again.' } }, 'No connection.');
    }
    if (res.status === 401) opts.onUnauthorized?.();
    if (!res.ok) {
      let parsed: ApiErrorBody | null = null;
      try {
        parsed = (await res.json()) as ApiErrorBody;
      } catch {
        parsed = null;
      }
      throw new ApiError(res.status, parsed, `Request failed (${res.status}).`, res.headers.get('retry-after'));
    }
    if (res.status === 204) return undefined as T;
    const type = res.headers.get('content-type') || '';
    if (type.includes('application/json')) return (await res.json()) as T;
    return (await res.blob()) as unknown as T;
  }

  return {
    base,
    get: <T>(path: string, init?: RequestInit) => request<T>('GET', path, undefined, init),
    post: <T>(path: string, body?: Json | FormData) => request<T>('POST', path, body ?? {}),
    put: <T>(path: string, body?: Json) => request<T>('PUT', path, body ?? {}),
    patch: <T>(path: string, body?: Json) => request<T>('PATCH', path, body ?? {}),
    del: <T>(path: string, body?: Json) => request<T>('DELETE', path, body),
    url: (path: string) => base + path,
  };
}

export type Client = ReturnType<typeof createClient>;
