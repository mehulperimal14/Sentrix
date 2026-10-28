import { ApiError } from '@sentrix/api';
import { Button, Check, ErrorState, Field, List, ListRow, RadioCards, SectionTitle, Select, Sheet, Spinner, Stepper, Strip, TextInput, formatDayTime, useToast } from '@sentrix/ui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronLeft } from 'lucide-react';
import { useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router';
import { api, useRefreshSession, useSession } from '../../lib/api';
import { useStepUp } from '../../lib/stepup';

function Page({ title, children }: { title: string; children: ReactNode }) {
  const { data: s } = useSession();
  const household = s?.role === 'owner' || s?.role === 'member';
  return (
    <div className="grid max-w-2xl gap-4 px-4 py-4 safe-x md:px-6">
      {household ? (
        <Link to="/settings" className="inline-flex items-center gap-1 justify-self-start text-[13px] font-semibold text-slate hover:text-graphite">
          <ChevronLeft aria-hidden className="size-4" /> Settings
        </Link>
      ) : null}
      <h1 className="text-[22px] font-bold">{title}</h1>
      {children}
    </div>
  );
}
function Card({ children }: { children: ReactNode }) {
  return <section className="grid gap-3 rounded-2xl bg-paper p-4">{children}</section>;
}
function messageOf(err: unknown) {
  return err instanceof ApiError ? err.message : 'That did not save. Try again.';
}

/* ------------------------------------------------------------ index */
export function SettingsIndex() {
  const { data: s } = useSession();
  const navigate = useNavigate();
  const owner = s?.role === 'owner';
  const rows = [
    { to: 'alerts', label: 'Alerts and PINs', sub: 'Answer window, safety net, Telegram, SOS and duress PINs', owner: false },
    { to: 'home', label: 'Home and address', sub: 'What responders get under WHERE', owner: false },
    { to: 'devices', label: 'Devices', sub: 'Name, room and floor of each device', owner: false },
    { to: 'account', label: 'Your account', sub: 'Name, e-mail, password, signed-in devices', owner: false },
    { to: 'privacy', label: 'Privacy', sub: 'Your choices, a copy of your data, closing the account', owner: false },
  ].filter((r) => owner || !r.owner);
  return (
    <div className="grid max-w-2xl gap-4 px-4 py-4 safe-x md:px-6">
      <h1 className="text-[22px] font-bold">Settings</h1>
      <List>
        {rows.map((r) => (
          <ListRow key={r.to} label={r.label} sub={r.sub} onClick={() => navigate(r.to)} />
        ))}
      </List>
      <SignOut />
    </div>
  );
}

function SignOut() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  return (
    <Button
      variant="quiet"
      onClick={async () => {
        await api.post('/api/v2/auth/logout', {}).catch(() => undefined);
        qc.clear();
        navigate('/login', { replace: true });
      }}
    >
      Sign out
    </Button>
  );
}

/* ------------------------------------------------------------ alerts */
export function AlertSettings() {
  const q = useQuery({ queryKey: ['settings'], queryFn: () => api.get<any>('/api/v2/settings') });
  const qc = useQueryClient();
  const toast = useToast();
  const stepUp = useStepUp();
  const [v, setV] = useState<any>(null);
  const [pins, setPins] = useState({ sos_pin: '', sos_pin_confirm: '', duress_pin: '', duress_pin_confirm: '' });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [tg, setTg] = useState<{ code: string; link: string | null } | null>(null);
  useEffect(() => {
    if (q.data && !v) setV({ ...q.data.sos, safety_off_ack: false, channels: { ...q.data.sos.channels } });
  }, [q.data, v]);
  if (q.isLoading || !v) return <Spinner />;
  if (!q.data) return <ErrorState message="Could not load settings." />;
  const d = q.data;
  const ro = !d.editable;

  async function saveSos(e: FormEvent) {
    e.preventDefault();
    setBusy('sos');
    setErrors({});
    try {
      await stepUp(() => api.put('/api/v2/settings/sos', v));
      toast('Alert settings saved.');
      qc.invalidateQueries({ queryKey: ['settings'] });
    } catch (err) {
      if (err instanceof ApiError) setErrors(err.fields);
      toast(messageOf(err));
    } finally {
      setBusy(null);
    }
  }
  async function savePins(e: FormEvent) {
    e.preventDefault();
    setBusy('pins');
    setErrors({});
    try {
      await stepUp(() => api.put('/api/v2/settings/pins', Object.fromEntries(Object.entries(pins).filter(([, x]) => x))));
      setPins({ sos_pin: '', sos_pin_confirm: '', duress_pin: '', duress_pin_confirm: '' });
      toast('PINs saved.');
      qc.invalidateQueries({ queryKey: ['settings'] });
    } catch (err) {
      if (err instanceof ApiError && err.code !== 'step_up_cancelled') {
        setErrors(err.fields);
        if (!Object.keys(err.fields).length) toast(err.message);
      }
    } finally {
      setBusy(null);
    }
  }
  async function linkTelegram() {
    try {
      const r = await api.post<{ code: string; link: string | null }>('/api/v2/telegram/link-code', {});
      setTg(r);
      if (r.link) window.open(r.link, '_blank', 'noopener');
    } catch (err) {
      toast(messageOf(err));
    }
  }
  async function unlink(id: string) {
    try {
      await api.del(`/api/v2/telegram/links/${id}`);
      toast('Telegram unlinked.');
      qc.invalidateQueries({ queryKey: ['settings'] });
    } catch (err) {
      toast(messageOf(err));
    }
  }
  const pin = (k: keyof typeof pins, label: string) => (
    <Field label={label} htmlFor={`s-${k}`} error={errors[k]}>
      <TextInput id={`s-${k}`} type="password" inputMode="numeric" autoComplete="off" maxLength={d.pins?.pin_length ?? 6} value={pins[k]} invalid={Boolean(errors[k])} onChange={(e) => setPins({ ...pins, [k]: e.target.value.replace(/\D/g, '') })} />
    </Field>
  );

  return (
    <Page title="Alerts and PINs">
      {ro ? <Strip tone="info">Only the account owner can change these.</Strip> : null}
      <form onSubmit={saveSos} noValidate className="grid gap-4">
        <Card>
          <SectionTitle>How long to wait for your answer</SectionTitle>
          <RadioCards name="window" value={v.window ?? d.window_choices[0]} onChange={(x: number) => setV({ ...v, window: x })} options={d.window_choices.map((m: number) => ({ value: m, label: `${m} minutes` }))} />
          <p className="text-[12px] text-slate">No answer is recorded as “Unanswered”, never as safe. Changes apply to the next alert.</p>
        </Card>
        <Card>
          <SectionTitle>Safety net</SectionTitle>
          <Check id="s-safety" checked={v.safety_defaults} disabled={ro} onChange={(x) => setV({ ...v, safety_defaults: x, safety_off_ack: false })} label={<b>On (recommended)</b>} sub="Your confirmed contacts are alerted even before you answer: at once when two different models agree, after 2 minutes for a fire, and if the device goes offline during an alert." />
          {!v.safety_defaults ? <Check id="s-ack" checked={v.safety_off_ack} onChange={(x) => setV({ ...v, safety_off_ack: x })} label="I understand that with the safety net off, my contacts hear nothing until I answer." error={errors.safety_off_ack} /> : null}
        </Card>
        <Card>
          <SectionTitle>How SENTRIX reaches you</SectionTitle>
          <Check id="s-email" checked={v.channels.email} disabled={ro} onChange={(x) => setV({ ...v, channels: { ...v.channels, email: x } })} label="E-mail" />
          <Check id="s-tg" checked={v.channels.telegram} disabled={ro} onChange={(x) => setV({ ...v, channels: { ...v.channels, telegram: x } })} label="Telegram" />
          <Check id="s-sms" checked={v.channels.sms} disabled={ro} onChange={(x) => setV({ ...v, channels: { ...v.channels, sms: x } })} label="SMS from the device" sub="When it is offline, or you haven’t seen the alert for 10 minutes." />
        </Card>
        {!ro ? (
          <Button type="submit" variant="primary" busy={busy === 'sos'}>
            Save alert settings
          </Button>
        ) : null}
      </form>

      {d.telegram ? (
        <Card>
          <SectionTitle>Telegram</SectionTitle>
          {!d.telegram.bot_configured ? <p className="text-[13px] text-slate">Telegram is not set up on this Portal.</p> : null}
          {d.telegram.links.length ? (
            <List>
              {d.telegram.links.map((l: { id: string; linked_at: string | null }) => (
                <ListRow key={l.id} label="Telegram chat" sub={l.linked_at ? `Linked ${formatDayTime(l.linked_at)}` : undefined} value={<button type="button" className="font-semibold text-chilli underline" onClick={() => unlink(l.id)}>Unlink</button>} />
              ))}
            </List>
          ) : null}
          {d.telegram.bot_configured ? (
            <Button block={false} size="sm" onClick={linkTelegram}>
              {d.telegram.links.length ? 'Link another chat' : 'Link Telegram'}
            </Button>
          ) : null}
          {tg ? (
            <Strip tone="info">
              If Telegram didn’t open, send <b className="select-all font-mono">/start {tg.code}</b> to the SENTRIX alerts bot. The code works once, for 24 hours.
            </Strip>
          ) : null}
        </Card>
      ) : null}

      {d.pins && !ro ? (
        <form onSubmit={savePins} noValidate>
          <Card>
            <SectionTitle>PINs</SectionTitle>
            <p className="text-[12.5px] text-slate">The SOS PIN marks a false alarm. If someone forces you to cancel, type the duress PIN instead: your screen shows “Marked as false alarm” while your contacts and the Desk are told to send help.</p>
            <List>
              <ListRow label="SOS PIN" value={d.pins.pin_set ? 'Set' : 'Not set'} tone={d.pins.pin_set ? 'ok' : 'bad'} />
              <ListRow label="Duress PIN" value={d.pins.duress_set ? 'Set' : 'Not set'} />
            </List>
            <div className="grid grid-cols-2 gap-2">
              {pin('sos_pin', 'New SOS PIN')}
              {pin('sos_pin_confirm', 'New SOS PIN again')}
            </div>
            {d.pins.contacts_exist ? (
              <div className="grid grid-cols-2 gap-2">
                {pin('duress_pin', 'New duress PIN')}
                {pin('duress_pin_confirm', 'New duress PIN again')}
              </div>
            ) : (
              <p className="text-[12px] text-slate">Add an emergency contact to set a duress PIN.</p>
            )}
            <Button type="submit" variant="primary" busy={busy === 'pins'}>
              Save PINs
            </Button>
          </Card>
        </form>
      ) : null}
    </Page>
  );
}

/* ------------------------------------------------------------ home */
export function HomeSettings() {
  const q = useQuery({ queryKey: ['household'], queryFn: () => api.get<any>('/api/v2/household') });
  const qc = useQueryClient();
  const toast = useToast();
  const [v, setV] = useState<any>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (q.data && !v) setV({ ...q.data.values });
  }, [q.data, v]);
  if (!v || !q.data) return <Spinner />;
  const ro = !q.data.editable;
  const set = (k: string) => (x: string) => setV({ ...v, [k]: x });
  const input = (k: string, label: string, opts: { optional?: boolean; help?: string; inputMode?: 'numeric' | 'tel' } = {}) => (
    <Field label={label} htmlFor={`h-${k}`} error={errors[k]} optional={opts.optional} help={opts.help}>
      <TextInput id={`h-${k}`} readOnly={ro} inputMode={opts.inputMode} value={v[k] ?? ''} invalid={Boolean(errors[k])} onChange={(e) => set(k)(e.target.value)} />
    </Field>
  );
  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    try {
      await api.put('/api/v2/household', v);
      toast('Home details saved. Devices get them on their next sync.');
      qc.invalidateQueries({ queryKey: ['household'] });
    } catch (err) {
      if (err instanceof ApiError) setErrors(err.fields);
      toast(messageOf(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Page title="Home and address">
      {ro ? <Strip tone="info">Only the account owner can change these.</Strip> : null}
      <form onSubmit={save} noValidate className="grid gap-4">
        <Card>
          <SectionTitle>Address</SectionTitle>
          <div className="grid grid-cols-2 gap-2">
            {input('house', 'House or flat')}
            {input('building', 'Building', { optional: true })}
          </div>
          {input('street', 'Street')}
          <div className="grid grid-cols-2 gap-2">
            {input('locality', 'Area')}
            {input('city', 'City')}
          </div>
          {input('landmark', 'Landmark')}
          <div className="grid grid-cols-2 gap-2">
            <Field label="State" htmlFor="h-state" error={errors.state}>
              <Select id="h-state" disabled={ro} value={v.state ?? ''} onChange={(e) => set('state')(e.target.value)}>
                <option value="">Choose…</option>
                {q.data.states.map((s: string) => (
                  <option key={s}>{s}</option>
                ))}
              </Select>
            </Field>
            {input('pincode', 'Postcode', { inputMode: 'numeric' })}
          </div>
          {input('entry_note', 'Entry note for responders', { optional: true, help: 'Never put door or gate codes here.' })}
        </Card>
        <Card>
          <SectionTitle>Who lives here</SectionTitle>
          <Stepper label="Adults" value={Number(v.adults ?? 1)} min={1} max={20} onChange={(x) => setV({ ...v, adults: x })} />
          <Stepper label="Seniors" value={Number(v.seniors ?? 0)} min={0} max={20} onChange={(x) => setV({ ...v, seniors: x })} />
          <Stepper label="Children" value={Number(v.children ?? 0)} min={0} max={20} onChange={(x) => setV({ ...v, children: x })} />
          {input('pets', 'Pets', { optional: true })}
        </Card>
        <Card>
          <SectionTitle>Nearest help</SectionTitle>
          {input('police_name', 'Police station', { optional: true })}
          {input('police_phone', 'Police station phone', { optional: true, inputMode: 'tel' })}
          {input('hospital_name', 'Hospital', { optional: true })}
          {input('hospital_phone', 'Hospital phone', { optional: true, inputMode: 'tel' })}
          {input('fire_name', 'Fire station', { optional: true })}
          {input('fire_phone', 'Fire station phone', { optional: true, inputMode: 'tel' })}
        </Card>
        <Card>
          <SectionTitle>Medical notes</SectionTitle>
          <Field label="Anything responders should know" htmlFor="h-medical" optional error={errors.medical}>
            <textarea id="h-medical" readOnly={ro} rows={3} value={v.medical ?? ''} onChange={(e) => set('medical')(e.target.value)} className="w-full rounded-xl border-[1.5px] border-line bg-paper px-3 py-2 text-[15px]" />
          </Field>
          <Check id="h-mc" checked={Boolean(v.medical_consent)} disabled={ro} onChange={(x) => setV({ ...v, medical_consent: x })} label="The people these notes are about agreed to them being stored" error={errors.medical_consent} />
          <Check id="h-mi" checked={Boolean(v.include_medical)} disabled={ro} onChange={(x) => setV({ ...v, include_medical: x })} label="Include them in help requests" />
        </Card>
        {!ro ? (
          <Button type="submit" variant="primary" busy={busy}>
            Save home details
          </Button>
        ) : null}
      </form>
    </Page>
  );
}

/* ------------------------------------------------------------ devices */
export function DeviceSettings() {
  const q = useQuery({ queryKey: ['settings'], queryFn: () => api.get<any>('/api/v2/settings') });
  const [open, setOpen] = useState<any>(null);
  if (q.isLoading || !q.data) return <Spinner />;
  return (
    <Page title="Devices">
      {q.data.devices.length ? (
        <List>
          {q.data.devices.map((d: any) => (
            <ListRow key={d.id} label={d.name ?? d.serial} sub={[d.room_label, d.floor ? `${d.floor} floor` : null, `Serial ${d.serial}`].filter(Boolean).join(', ')} value={d.online ? 'Online' : d.claimed ? 'Offline' : 'Not added'} tone={d.online ? 'ok' : 'warn'} onClick={q.data.editable && d.claimed ? () => setOpen(d) : undefined} />
          ))}
        </List>
      ) : (
        <Strip tone="info">
          No device yet. <Link to="/setup/device" className="font-semibold underline">Add the device</Link>
        </Strip>
      )}
      <p className="text-[12.5px] text-slate">Faces, recordings for training and Wi-Fi are set up on the device itself, from a computer on the same network.</p>
      <Sheet open={Boolean(open)} onClose={() => setOpen(null)} title={open?.name ?? open?.serial}>
        {open ? <DeviceForm device={open} onDone={() => setOpen(null)} /> : null}
      </Sheet>
    </Page>
  );
}

function DeviceForm({ device, onDone }: { device: any; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const stepUp = useStepUp();
  const [v, setV] = useState({ name: device.name ?? '', room_label: device.room_label ?? '', floor: device.floor ?? 'Ground' });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [confirmUnpair, setConfirmUnpair] = useState(false);
  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api.patch(`/api/v2/devices/${device.id}`, v);
      toast('Device saved.');
      await Promise.all([qc.invalidateQueries({ queryKey: ['settings'] }), qc.invalidateQueries({ queryKey: ['home'] })]);
      onDone();
    } catch (err) {
      if (err instanceof ApiError) setErrors(err.fields);
      toast(messageOf(err));
    } finally {
      setBusy(false);
    }
  }
  async function unpair() {
    setBusy(true);
    try {
      const r = await stepUp(() => api.post<{ message?: string }>(`/api/v2/devices/${device.id}/unpair`, {}));
      toast(r.message ?? 'Device removed from your home.');
      await Promise.all([qc.invalidateQueries({ queryKey: ['settings'] }), qc.invalidateQueries({ queryKey: ['home'] })]);
      onDone();
    } catch (err) {
      if (!(err instanceof ApiError && err.code === 'step_up_cancelled')) toast(messageOf(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={save} noValidate className="grid gap-3">
      <Field label="Name" htmlFor="d-name" error={errors.name}>
        <TextInput id="d-name" value={v.name} onChange={(e) => setV({ ...v, name: e.target.value })} />
      </Field>
      <Field label="Room" htmlFor="d-room" error={errors.room_label} help="Alerts name the room.">
        <TextInput id="d-room" value={v.room_label} onChange={(e) => setV({ ...v, room_label: e.target.value })} />
      </Field>
      <Field label="Floor" htmlFor="d-floor" error={errors.floor}>
        <Select id="d-floor" value={v.floor} onChange={(e) => setV({ ...v, floor: e.target.value })}>
          {['Ground', 'First', 'Second', 'Third or higher'].map((f) => (
            <option key={f}>{f}</option>
          ))}
        </Select>
      </Field>
      <Button type="submit" variant="primary" busy={busy}>
        Save device
      </Button>
      {confirmUnpair ? (
        <Strip tone="alarm">
          <p className="mb-2">The device stops sending alerts to you. Adding it again needs a new device code from SENTRIX.</p>
          <Button variant="alarm" size="sm" block={false} busy={busy} onClick={unpair}>
            Remove it from my home
          </Button>
        </Strip>
      ) : (
        <Button variant="quiet" onClick={() => setConfirmUnpair(true)}>
          Remove this device from my home…
        </Button>
      )}
    </form>
  );
}

/* ------------------------------------------------------------ account */
export function AccountSettings() {
  const q = useQuery({ queryKey: ['account'], queryFn: () => api.get<any>('/api/v2/account') });
  const sessions = useQuery({ queryKey: ['sessions'], queryFn: () => api.get<{ sessions: any[] }>('/api/v2/account/sessions') });
  const { data: s } = useSession();
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const [v, setV] = useState<any>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (q.data && !v) setV({ display_name: q.data.display_name ?? '', email: q.data.email ?? '' });
  }, [q.data, v]);
  if (!q.data || !v) return <Spinner />;
  const a = q.data;
  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    try {
      await api.patch('/api/v2/account', v);
      toast('Saved.');
      qc.invalidateQueries({ queryKey: ['account'] });
      qc.invalidateQueries({ queryKey: ['session'] });
    } catch (err) {
      if (err instanceof ApiError) setErrors(err.fields);
      toast(messageOf(err));
    } finally {
      setBusy(false);
    }
  }
  async function revoke(id: string) {
    try {
      const r = await api.del<{ signed_out: boolean }>(`/api/v2/account/sessions/${encodeURIComponent(id)}`);
      if (r.signed_out) navigate('/login', { replace: true });
      else sessions.refetch();
    } catch (err) {
      toast(messageOf(err));
    }
  }
  async function revokeOthers() {
    try {
      const r = await api.post<{ revoked: number }>('/api/v2/account/sessions/revoke-others', {});
      toast(`${r.revoked} other ${r.revoked === 1 ? 'device was' : 'devices were'} signed out.`);
      sessions.refetch();
    } catch (err) {
      toast(messageOf(err));
    }
  }
  return (
    <Page title="Your account">
      <form onSubmit={save} noValidate>
        <Card>
          <List>
            <ListRow label="Username" value={a.username} />
          </List>
          <Field label="Your name" htmlFor="a-name" error={errors.display_name}>
            <TextInput id="a-name" autoComplete="name" value={v.display_name} onChange={(e) => setV({ ...v, display_name: e.target.value })} />
          </Field>
          <Field label="E-mail" htmlFor="a-email" error={errors.email} help={a.email ? (a.email_verified ? 'Confirmed.' : 'Not confirmed yet.') : undefined}>
            <TextInput id="a-email" type="email" inputMode="email" autoComplete="email" value={v.email} onChange={(e) => setV({ ...v, email: e.target.value })} />
          </Field>
          {a.email && !a.email_verified ? (
            <button type="button" className="justify-self-start text-[13px] font-semibold text-indigo underline" onClick={() => api.post('/api/v2/account/email/resend', {}).then(() => toast('Confirmation e-mail sent.'), (e) => toast(messageOf(e)))}>
              Send the confirmation e-mail again
            </button>
          ) : null}
          <Button type="submit" variant="primary" busy={busy}>
            Save
          </Button>
        </Card>
      </form>
      <PasswordCard min={s?.passwordMin ?? 10} />
      <Card>
        <SectionTitle>Two-factor sign-in</SectionTitle>
        <p className="text-[13px] text-slate">{a.two_factor === 'on' ? 'On: you type a code from your authenticator app when you sign in.' : a.two_factor === 'required' ? 'Required for your role.' : 'Not used for household accounts.'}</p>
        {a.two_factor !== 'not_used' ? (
          <Button block={false} size="sm" onClick={() => navigate('/account/2fa')}>
            Manage two-factor
          </Button>
        ) : null}
      </Card>
      <Card>
        <SectionTitle>Where you are signed in</SectionTitle>
        <List>
          {(sessions.data?.sessions ?? []).map((x) => (
            <ListRow key={x.id} label={x.current ? 'This browser' : (x.user_agent ?? 'A browser').slice(0, 60)} sub={x.last_seen_at ? `Last used ${formatDayTime(x.last_seen_at, s?.timezone)}` : undefined} value={<button type="button" className="font-semibold text-chilli underline" onClick={() => revoke(x.id)}>{x.current ? 'Sign out' : 'Sign it out'}</button>} />
          ))}
        </List>
        <Button size="sm" block={false} onClick={revokeOthers}>
          Sign out everywhere else
        </Button>
      </Card>
    </Page>
  );
}

function PasswordCard({ min }: { min: number }) {
  const toast = useToast();
  const [v, setV] = useState({ current_password: '', password: '', password_confirm: '' });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    try {
      await api.post('/api/v2/account/password', v);
      setV({ current_password: '', password: '', password_confirm: '' });
      toast('Password changed. Other devices were signed out.');
    } catch (err) {
      if (err instanceof ApiError) setErrors(Object.keys(err.fields).length ? err.fields : { current_password: err.message });
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={save} noValidate>
      <Card>
        <SectionTitle>Password</SectionTitle>
        <Field label="Current password" htmlFor="p-cur" error={errors.current_password}>
          <TextInput id="p-cur" type="password" autoComplete="current-password" value={v.current_password} onChange={(e) => setV({ ...v, current_password: e.target.value })} />
        </Field>
        <Field label="New password" htmlFor="p-new" error={errors.password} help={`At least ${min} characters.`}>
          <TextInput id="p-new" type="password" autoComplete="new-password" value={v.password} onChange={(e) => setV({ ...v, password: e.target.value })} />
        </Field>
        <Field label="New password again" htmlFor="p-new2" error={errors.password_confirm}>
          <TextInput id="p-new2" type="password" autoComplete="new-password" value={v.password_confirm} onChange={(e) => setV({ ...v, password_confirm: e.target.value })} />
        </Field>
        <Button type="submit" busy={busy}>
          Change password
        </Button>
      </Card>
    </form>
  );
}

/* ------------------------------------------------------------ privacy */
export function PrivacySettings() {
  const q = useQuery({ queryKey: ['privacy'], queryFn: () => api.get<any>('/api/v2/privacy') });
  const qc = useQueryClient();
  const toast = useToast();
  const stepUp = useStepUp();
  const refresh = useRefreshSession();
  const [c, setC] = useState<Record<string, boolean> | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [erase, setErase] = useState('');
  const [eraseError, setEraseError] = useState<string>();
  const { data: s } = useSession();
  useEffect(() => {
    if (q.data && !c) setC({ ...q.data.consents });
  }, [q.data, c]);
  if (!q.data || !c) return <Spinner />;
  const d = q.data;
  const ro = !d.editable;

  async function saveConsents() {
    setBusy('consents');
    try {
      await api.put('/api/v2/privacy/consents', Object.fromEntries(Object.entries(c!).map(([k, x]) => [`consent_${k}`, x])));
      toast('Your choices are saved.');
      qc.invalidateQueries({ queryKey: ['privacy'] });
    } catch (err) {
      toast(messageOf(err));
    } finally {
      setBusy(null);
    }
  }
  async function exportData() {
    setBusy('export');
    try {
      const res = await stepUp(async () => {
        const r = await fetch('/api/v2/privacy/export', { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': s?.csrf ?? '' } });
        if (!r.ok) throw new ApiError(r.status, await r.json().catch(() => null), 'The export did not work.');
        return r;
      });
      const blob = await res.blob();
      const name = /filename="?([^";]+)/.exec(res.headers.get('Content-Disposition') ?? '')?.[1] ?? 'sentrix-export.json';
      const url = URL.createObjectURL(blob);
      const link = Object.assign(document.createElement('a'), { href: url, download: name });
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      if (!(err instanceof ApiError && err.code === 'step_up_cancelled')) toast(messageOf(err));
    } finally {
      setBusy(null);
    }
  }
  async function scheduleErasure() {
    setBusy('erase');
    setEraseError(undefined);
    try {
      const r = await stepUp(() => api.post<{ message: string }>('/api/v2/privacy/erase', { confirm: erase }));
      toast(r.message);
      setErase('');
      qc.invalidateQueries({ queryKey: ['privacy'] });
      refresh();
    } catch (err) {
      if (!(err instanceof ApiError && err.code === 'step_up_cancelled')) setEraseError(messageOf(err));
    } finally {
      setBusy(null);
    }
  }
  async function cancelErasure() {
    const r = await api.post<{ message: string }>('/api/v2/privacy/erase/cancel', {});
    toast(r.message);
    qc.invalidateQueries({ queryKey: ['privacy'] });
    refresh();
  }

  return (
    <Page title="Privacy">
      <Card>
        <SectionTitle>Your choices</SectionTitle>
        {d.notice.purposes.map((p: any) => (
          <Check key={p.key} id={`pv-${p.key}`} checked={Boolean(c[p.key])} disabled={ro || p.key === 'account_sos'} onChange={(x) => setC({ ...c, [p.key]: x })} label={<b>{p.title}</b>} sub={p.text} />
        ))}
        <p className="text-[12px] text-slate">Withdrawing is as easy as agreeing. The alert service itself ends only by closing the account. Notice version {d.notice.notice_version}.</p>
        {!ro ? (
          <Button variant="primary" busy={busy === 'consents'} onClick={saveConsents}>
            Save my choices
          </Button>
        ) : null}
        <Link to="/privacy/notice" className="text-[13px] font-semibold text-indigo underline">
          Read the privacy notice
        </Link>
      </Card>
      {!ro ? (
        <>
          <Card>
            <SectionTitle>A copy of your data</SectionTitle>
            <p className="text-[13px] text-slate">Everything the SENTRIX server holds about your home, as one file. Faces, video and evidence stay on the device and are not in it.</p>
            <Button block={false} busy={busy === 'export'} onClick={exportData}>
              Download my data
            </Button>
          </Card>
          <Card>
            <SectionTitle>Close the account</SectionTitle>
            {d.erasure_due ? (
              <>
                <Strip tone="alarm">Everything will be erased on {formatDayTime(d.erasure_due, s?.timezone)}.</Strip>
                <Button variant="primary" onClick={cancelErasure}>
                  Keep my account
                </Button>
              </>
            ) : (
              <>
                <p className="text-[13px] text-slate">Your account, home details and contacts are erased after {d.grace_days} days. Until then you can change your mind. Alerts stop at once.</p>
                <Field label="Type ERASE to confirm" htmlFor="erase" error={eraseError}>
                  <TextInput id="erase" autoComplete="off" value={erase} onChange={(e) => setErase(e.target.value)} />
                </Field>
                <Button variant="alarm" block={false} busy={busy === 'erase'} disabled={erase.trim().toUpperCase() !== 'ERASE'} onClick={scheduleErasure}>
                  Close my account
                </Button>
              </>
            )}
          </Card>
        </>
      ) : null}
    </Page>
  );
}
