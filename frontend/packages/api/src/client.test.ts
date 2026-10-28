import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, createClient } from './client';

afterEach(() => vi.restoreAllMocks());

function mockFetch(status: number, body: unknown) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }),
  );
}

describe('api client', () => {
  it('sends the CSRF header only on state-changing calls', async () => {
    const f = mockFetch(200, { ok: true });
    const api = createClient({ csrf: () => 'tok' });
    await api.get('/api/v2/session');
    await api.post('/api/v2/x', { a: 1 });
    const getHeaders = f.mock.calls[0][1]!.headers as Record<string, string>;
    const postHeaders = f.mock.calls[1][1]!.headers as Record<string, string>;
    expect(getHeaders['X-CSRF-Token']).toBeUndefined();
    expect(postHeaders['X-CSRF-Token']).toBe('tok');
    expect(postHeaders['Content-Type']).toBe('application/json');
  });

  it('turns the error model into ApiError with field messages', async () => {
    mockFetch(422, { error: { code: 'validation', message: 'Check the form.', fields: { phone: 'Enter a 10-digit number.' } } });
    const api = createClient();
    const err = (await api.post("/api/v2/contacts", {}).catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe('validation');
    expect(err.fields.phone).toBe('Enter a 10-digit number.');
  });

  it('calls onUnauthorized on 401 and prefixes the tunnel base', async () => {
    const f = mockFetch(401, { error: { code: 'not_signed_in' } });
    const spy = vi.fn();
    const api = createClient({ base: '/d/abc', onUnauthorized: spy });
    await api.get('/api/health').catch(() => undefined);
    expect(f.mock.calls[0][0]).toBe('/d/abc/api/health');
    expect(spy).toHaveBeenCalled();
  });
});
