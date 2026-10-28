import { ApiError } from '@sentrix/api';
import { Button, Field, Spinner, Strip, TextInput } from '@sentrix/ui';
import { useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { Navigate, useLocation, useNavigate, useSearchParams } from 'react-router';
import { api, useSession } from '../../lib/api';

/** /claim?sn=SERIAL#code=CODE - where the QR code on a device label leads.
 *  During setup it fills the Device step; afterwards it adds the device. */
export function Claim() {
  const [params] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { data: s, isLoading } = useSession();
  const fromHash = new URLSearchParams(location.hash.replace(/^#/, '')).get('code') ?? '';
  const [serial, setSerial] = useState(params.get('sn') ?? '');
  const [code, setCode] = useState(fromHash || params.get('code') || '');
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string>();
  const [busy, setBusy] = useState(false);
  if (isLoading) return <Spinner />;
  if (!s?.signedIn) return <Navigate to={`/login?next=${encodeURIComponent(`/claim?sn=${serial}${fromHash ? `&code=${fromHash}` : ''}`)}`} replace />;
  if (s.role === 'owner' && s.household && !s.household.setupDone) {
    return <Navigate to={`/setup/device?sn=${encodeURIComponent(serial)}&code=${encodeURIComponent(code)}`} replace />;
  }
  if (s.role !== 'owner') return <Strip tone="info">Only the account owner can add a device.</Strip>;

  async function claim(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    setMessage(undefined);
    try {
      await api.post('/api/v2/claim', { serial, claim_code: code });
      await qc.invalidateQueries({ queryKey: ['home'] });
      navigate('/settings/devices', { replace: true });
    } catch (err) {
      if (err instanceof ApiError) {
        setErrors(err.fields);
        if (!Object.keys(err.fields).length) setMessage(err.message);
      } else setMessage('That did not work.');
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={claim} noValidate className="mx-auto grid max-w-md gap-4 px-4 py-6">
      <h1 className="text-[22px] font-bold">Add a device</h1>
      {message ? <Strip tone="alarm" role="alert">{message}</Strip> : null}
      <Field label="Serial number" htmlFor="cl-sn" error={errors.serial}>
        <TextInput id="cl-sn" autoCapitalize="characters" value={serial} onChange={(e) => setSerial(e.target.value)} />
      </Field>
      <Field label="Device code" htmlFor="cl-code" error={errors.claim_code} help="8 letters on the label.">
        <TextInput id="cl-code" autoCapitalize="characters" value={code} onChange={(e) => setCode(e.target.value)} />
      </Field>
      <Button type="submit" variant="primary" busy={busy}>
        Add this device
      </Button>
    </form>
  );
}
