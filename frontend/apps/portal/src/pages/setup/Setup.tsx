import { ApiError } from '@sentrix/api';
import { Button, Check, ErrorState, Field, List, ListRow, RadioCards, SectionTitle, Select, Spinner, Stepper, Strip, TextInput, cn, useToast } from '@sentrix/ui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react';
import { Navigate, useNavigate, useParams, useSearchParams } from 'react-router';
import { api, useRefreshSession, useSession } from '../../lib/api';
import { ContactForm, ContactRows, type ContactOut } from '../owner/contacts';

/* The Experience Plan's five steps over the Portal's seven (the server keeps
   its rules and messages): You and your address = 1+2, People = 3+4,
   Device = 5, Alerts = 6, Review = 7. */
const STEPS = [
  { slug: 'you', title: 'You and your address', backend: [1, 2] },
  { slug: 'people', title: 'People', backend: [3, 4] },
  { slug: 'device', title: 'Device', backend: [5] },
  { slug: 'alerts', title: 'Alerts', backend: [6] },
  { slug: 'review', title: 'Review', backend: [7] },
] as const;

interface Wizard {
  current_step: number;
  completed: boolean;
  reachable_upto: number;
  steps: { number: number; title: string; done: boolean; reachable: boolean }[];
}

function uiIndexFor(backendStep: number) {
  return Math.max(0, STEPS.findIndex((s) => (s.backend as readonly number[]).includes(backendStep)));
}

export function Setup() {
  const { step } = useParams();
  const navigate = useNavigate();
  const wizard = useQuery({ queryKey: ['wizard'], queryFn: () => api.get<Wizard>('/api/v2/onboarding') });
  if (wizard.isLoading) return <Spinner />;
  if (wizard.isError || !wizard.data) return <div className="p-4"><ErrorState message="Could not load setup." onRetry={() => wizard.refetch()} /></div>;
  const w = wizard.data;
  const reachableUi = uiIndexFor(w.reachable_upto);
  const index = step ? STEPS.findIndex((s) => s.slug === step) : -1;
  if (step === 'done') return <SetupDone />;
  if (index < 0) return <Navigate to={`/setup/${STEPS[w.completed ? 0 : uiIndexFor(w.current_step)].slug}`} replace />;
  if (index > reachableUi && !w.completed) return <Navigate to={`/setup/${STEPS[reachableUi].slug}`} replace />;
  const doneUi = (i: number) => STEPS[i].backend.every((b) => w.steps.find((s) => s.number === b)?.done);
  const go = (i: number) => navigate(i >= STEPS.length ? '/setup/done' : `/setup/${STEPS[i].slug}`);

  return (
    <div className="mx-auto grid w-full max-w-xl gap-4 px-4 py-4 safe-x">
      <div className="grid gap-2">
        <p className="text-[13px] font-semibold text-slate">
          Set up your home. Step {index + 1} of {STEPS.length}: {STEPS[index].title}
        </p>
        <ol className="grid grid-cols-5 gap-1" aria-label="Setup steps">
          {STEPS.map((s, i) => (
            <li key={s.slug}>
              <button
                type="button"
                disabled={i > reachableUi && !w.completed}
                onClick={() => go(i)}
                aria-current={i === index ? 'step' : undefined}
                aria-label={`Step ${i + 1}: ${s.title}${doneUi(i) ? ', done' : ''}`}
                className={cn('block h-1.5 w-full rounded-full', i === index ? 'bg-indigo-fill' : doneUi(i) ? 'bg-neem' : 'bg-hatch')}
              />
            </li>
          ))}
        </ol>
      </div>
      {index === 0 ? <StepYou onDone={() => go(1)} /> : null}
      {index === 1 ? <StepPeople onDone={() => go(2)} /> : null}
      {index === 2 ? <StepDevice onDone={() => go(3)} /> : null}
      {index === 3 ? <StepAlerts onDone={() => go(4)} /> : null}
      {index === 4 ? <StepReview onDone={() => go(5)} /> : null}
    </div>
  );
}

function Card({ children }: { children: ReactNode }) {
  return <div className="grid gap-3 rounded-2xl bg-paper p-4">{children}</div>;
}

function useStep<T>(n: number) {
  return useQuery({ queryKey: ['onboarding', n], queryFn: () => api.get<T>(`/api/v2/onboarding/${n}`) });
}

function errorsOf(e: unknown): { fields: Record<string, string>; message?: string } {
  if (e instanceof ApiError) return { fields: e.fields, message: Object.keys(e.fields).length ? undefined : e.message };
  return { fields: {}, message: 'That did not save. Try again.' };
}

/* ------------------------------------------------------------ 1 + 2 */
function StepYou({ onDone }: { onDone: () => void }) {
  const one = useStep<any>(1);
  const two = useStep<any>(2);
  const qc = useQueryClient();
  const [you, setYou] = useState<any>(null);
  const [addr, setAddr] = useState<any>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string>();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (one.data && !you) setYou({ ...one.data.values });
  }, [one.data, you]);
  useEffect(() => {
    if (two.data && !addr) setAddr({ ...two.data.values });
  }, [two.data, addr]);
  if (!you || !addr) return <Spinner />;
  const a = (k: string) => (v: string) => setAddr((x: any) => ({ ...x, [k]: v }));

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    setMessage(undefined);
    try {
      await api.put('/api/v2/onboarding/1', you);
    } catch (err) {
      const r = errorsOf(err);
      setErrors(r.fields);
      setMessage(r.message);
      setBusy(false);
      return;
    }
    try {
      await api.put('/api/v2/onboarding/2', addr);
      await qc.invalidateQueries({ queryKey: ['wizard'] });
      onDone();
    } catch (err) {
      const r = errorsOf(err);
      setErrors(r.fields);
      setMessage(r.message);
    } finally {
      setBusy(false);
    }
  }
  const input = (id: string, label: string, value: string | null | undefined, on: (v: string) => void, opts: { help?: string; type?: string; inputMode?: 'tel' | 'email' | 'numeric'; optional?: boolean; autoComplete?: string } = {}) => (
    <Field label={label} htmlFor={`f-${id}`} error={errors[id]} help={opts.help} optional={opts.optional}>
      <TextInput id={`f-${id}`} type={opts.type ?? 'text'} inputMode={opts.inputMode} autoComplete={opts.autoComplete ?? 'off'} value={value ?? ''} invalid={Boolean(errors[id])} onChange={(e) => on(e.target.value)} />
    </Field>
  );
  return (
    <form onSubmit={save} noValidate className="grid gap-4">
      <h1 className="text-[22px] font-bold">You and your address</h1>
      <p className="-mt-2 text-[13.5px] text-slate">Responders use the address and the entry note to find the door.</p>
      {message ? <Strip tone="alarm" role="alert">{message}</Strip> : null}
      <Card>
        {input('display_name', 'Your name', you.display_name, (v) => setYou({ ...you, display_name: v }), { autoComplete: 'name' })}
        {input('phone', 'Mobile number', you.phone, (v) => setYou({ ...you, phone: v }), { type: 'tel', inputMode: 'tel', autoComplete: 'tel' })}
        {input('email', 'E-mail', you.email, (v) => setYou({ ...you, email: v }), { type: 'email', inputMode: 'email', autoComplete: 'email' })}
        <Check id="adult" checked={Boolean(you.adult)} onChange={(v) => setYou({ ...you, adult: v })} label="I am 18 or older" error={errors.adult} />
      </Card>
      <Card>
        <div className="grid grid-cols-2 gap-2">
          {input('house', 'House or flat', addr.house, a('house'))}
          {input('building', 'Building', addr.building, a('building'), { optional: true })}
        </div>
        {input('street', 'Street', addr.street, a('street'))}
        <div className="grid grid-cols-2 gap-2">
          {input('locality', 'Area', addr.locality, a('locality'))}
          {input('city', 'City', addr.city, a('city'))}
        </div>
        {input('landmark', 'Landmark', addr.landmark, a('landmark'), { help: 'Write it as you would say it, for example “Opposite the SBI ATM”.' })}
        <div className="grid grid-cols-2 gap-2">
          <Field label="State" htmlFor="f-state" error={errors.state}>
            <Select id="f-state" value={addr.state ?? ''} onChange={(e) => a('state')(e.target.value)}>
              <option value="">Choose…</option>
              {(two.data?.states ?? []).map((s: string) => (
                <option key={s}>{s}</option>
              ))}
            </Select>
          </Field>
          {input('pincode', 'Postcode', addr.pincode, a('pincode'), { inputMode: 'numeric' })}
        </div>
        {input('entry_note', 'Entry note for responders', addr.entry_note, a('entry_note'), { optional: true, help: 'How to find the door. Never put door or gate codes here.' })}
        <details className="text-[13.5px]">
          <summary className="cursor-pointer font-semibold">Nearest police station and hospital (optional)</summary>
          <div className="mt-3 grid gap-2">
            {input('police_name', 'Police station', addr.police_name, a('police_name'), { optional: true })}
            {input('hospital_name', 'Hospital', addr.hospital_name, a('hospital_name'), { optional: true })}
            {input('fire_name', 'Fire station', addr.fire_name, a('fire_name'), { optional: true })}
          </div>
        </details>
      </Card>
      <Button type="submit" variant="primary" busy={busy}>
        Save and continue
      </Button>
    </form>
  );
}

/* ------------------------------------------------------------ 3 + 4 */
function StepPeople({ onDone }: { onDone: () => void }) {
  const three = useStep<any>(3);
  const four = useStep<any>(4);
  const qc = useQueryClient();
  const toast = useToast();
  const [h, setH] = useState<any>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  useEffect(() => {
    if (three.data && !h) setH({ adults: 1, seniors: 0, children: 0, ...three.data.values });
  }, [three.data, h]);
  if (!h || !four.data) return <Spinner />;
  const contacts: ContactOut[] = four.data.contacts?.contacts ?? [];
  const canAdd = four.data.contacts?.can_add ?? contacts.length < 3;

  async function save() {
    setBusy(true);
    setErrors({});
    setMessage(undefined);
    try {
      await api.put('/api/v2/onboarding/3', h);
      await api.post('/api/v2/onboarding/4', {});
      await qc.invalidateQueries({ queryKey: ['wizard'] });
      onDone();
    } catch (err) {
      const r = errorsOf(err);
      setErrors(r.fields);
      setMessage(r.message ?? (r.fields ? Object.values(r.fields)[0] : undefined));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="grid gap-4">
      <h1 className="text-[22px] font-bold">People</h1>
      <p className="-mt-2 text-[13.5px] text-slate">Who lives here, and who SENTRIX may alert when you ask for help.</p>
      {message ? <Strip tone="alarm" role="alert">{message}</Strip> : null}
      <Card>
        <SectionTitle>Who lives here</SectionTitle>
        <Stepper label="Adults" value={h.adults ?? 1} min={1} max={20} onChange={(v) => setH({ ...h, adults: v })} />
        <Stepper label="Seniors" value={h.seniors ?? 0} min={0} max={20} onChange={(v) => setH({ ...h, seniors: v })} />
        <Stepper label="Children" value={h.children ?? 0} min={0} max={20} onChange={(v) => setH({ ...h, children: v })} />
        <Field label="Pets" htmlFor="f-pets" optional error={errors.pets}>
          <TextInput id="f-pets" value={h.pets ?? ''} onChange={(e) => setH({ ...h, pets: e.target.value })} placeholder="A dog" />
        </Field>
        <p className="text-[12px] text-slate">Children’s faces are never added. You can invite adults to the app later from People.</p>
      </Card>
      <Card>
        <SectionTitle>Emergency contacts</SectionTitle>
        {contacts.length ? <ContactRows contacts={contacts} /> : <Strip tone="info">No emergency contacts yet.</Strip>}
        {adding ? (
          <ContactForm
            onCancel={() => setAdding(false)}
            onSaved={(r) => {
              setAdding(false);
              toast(r.emailed ? `Link e-mailed to ${r.contact.name}. You can also copy it from People.` : `${r.contact.name} added. Send them the link from People.`);
              qc.invalidateQueries({ queryKey: ['onboarding', 4] });
            }}
          />
        ) : canAdd ? (
          <Button onClick={() => setAdding(true)}>Add an emergency contact</Button>
        ) : null}
        <p className="text-[12px] text-slate">Contacts don’t have to confirm before you finish. Until they do, alerts reach only you.</p>
      </Card>
      <Button variant="primary" busy={busy} onClick={save}>
        Continue
      </Button>
    </div>
  );
}

/* ------------------------------------------------------------ 5 */
interface DeviceStep {
  serial: string;
  assigned: { id: string; serial: string }[];
  claimed: { id: string; serial: string; name: string | null; room_label: string | null; floor: string | null; online: boolean }[];
}

function StepDevice({ onDone }: { onDone: () => void }) {
  const q = useStep<DeviceStep>(5);
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const [manual, setManual] = useState(Boolean(params.get('sn')));
  const [serial, setSerial] = useState(params.get('sn') ?? '');
  const [code, setCode] = useState(params.get('code') ?? '');
  const [claimMsg, setClaimMsg] = useState<string>();
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [room, setRoom] = useState('Living room');
  const [otherRoom, setOtherRoom] = useState('');
  const [name, setName] = useState('');
  const [floor, setFloor] = useState('Ground');
  const [mode, setMode] = useState<'home' | 'armed'>('home');
  const [viewChecked, setViewChecked] = useState(false);
  const device = q.data?.claimed?.[0];
  useEffect(() => {
    if (device) {
      if (device.room_label) setRoom(device.room_label);
      if (device.name) setName(device.name);
      if (device.floor) setFloor(device.floor);
    }
  }, [device?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  if (q.isLoading || !q.data) return <Spinner />;
  const d = q.data;

  async function claim(body: { serial?: string; claim_code?: string }) {
    setBusy(true);
    setClaimMsg(undefined);
    setErrors({});
    try {
      await api.post('/api/v2/onboarding/5/claim', body);
      await qc.invalidateQueries({ queryKey: ['onboarding', 5] });
    } catch (err) {
      const r = errorsOf(err);
      setErrors(r.fields);
      setClaimMsg(r.message ?? Object.values(r.fields)[0]);
    } finally {
      setBusy(false);
    }
  }

  async function saveDevice(e: FormEvent) {
    e.preventDefault();
    if (!device) return;
    const roomLabel = room === 'Other' ? otherRoom.trim() : room;
    setBusy(true);
    setErrors({});
    try {
      await api.put(`/api/v2/onboarding/5/devices/${device.id}`, { name: name || `${roomLabel} unit`, room_label: roomLabel, floor, mode, view_checked: viewChecked });
      await qc.invalidateQueries({ queryKey: ['wizard'] });
      onDone();
    } catch (err) {
      const r = errorsOf(err);
      setErrors(r.fields);
      setClaimMsg(r.message ?? Object.values(r.fields)[0]);
    } finally {
      setBusy(false);
    }
  }

  if (!device) {
    const assigned = d.assigned?.[0];
    return (
      <div className="grid gap-4">
        <h1 className="text-[22px] font-bold">Add your device</h1>
        {claimMsg ? <Strip tone="alarm" role="alert">{claimMsg}</Strip> : null}
        {assigned && !manual ? (
          <Card>
            <p className="text-[13.5px] text-slate">SENTRIX assigned this device to your account. Adding it proves it is in your home.</p>
            <List>
              <ListRow label="SENTRIX unit" sub={`Serial ${assigned.serial}`} value="Waiting for you" />
            </List>
            <p className="text-[13.5px]">Type the device code from the label on the device, or scan its QR code with your phone camera.</p>
            <Field label="Device code" htmlFor="claim-code" help="8 letters on the label. Capitals and the dash don’t matter." error={errors.claim_code}>
              <TextInput id="claim-code" autoCapitalize="characters" value={code} invalid={Boolean(errors.claim_code)} onChange={(e) => setCode(e.target.value)} placeholder="KTRW-BQXL" />
            </Field>
            <Button variant="primary" busy={busy} onClick={() => claim({ serial: assigned.serial, claim_code: code })}>
              Add this device
            </Button>
            <button type="button" className="justify-self-start text-[13px] font-semibold text-indigo underline" onClick={() => setManual(true)}>
              Enter a different serial number
            </button>
          </Card>
        ) : (
          <Card>
            <p className="text-[13.5px] text-slate">Type the serial number and device code from the label on the device, or scan its QR code.</p>
            <Field label="Serial number" htmlFor="claim-serial" help="On the label and in your welcome letter." error={errors.serial}>
              <TextInput id="claim-serial" autoCapitalize="characters" value={serial} invalid={Boolean(errors.serial)} onChange={(e) => setSerial(e.target.value)} placeholder="SX5-000123" />
            </Field>
            <Field label="Device code" htmlFor="claim-code2" help="8 letters on the label." error={errors.claim_code}>
              <TextInput id="claim-code2" autoCapitalize="characters" value={code} invalid={Boolean(errors.claim_code)} onChange={(e) => setCode(e.target.value)} placeholder="KTRW-BQXL" />
            </Field>
            <Button variant="primary" busy={busy} onClick={() => claim({ serial, claim_code: code })}>
              Add this device
            </Button>
          </Card>
        )}
        <Button variant="quiet" onClick={onDone}>
          I don’t have the device yet: add it later
        </Button>
      </div>
    );
  }

  return (
    <form onSubmit={saveDevice} noValidate className="grid gap-4">
      <h1 className="text-[22px] font-bold">{device.online ? 'Added to your home' : 'Added to your account'}</h1>
      {device.online ? (
        <Strip tone="ok">
          <b>The green light on the device is now steady.</b> It is connected and belongs to your home.
        </Strip>
      ) : (
        <Strip tone="warn">
          <b>The device isn’t online yet.</b> Plug it in. If its amber light stays on, it can’t reach Wi-Fi: connect it with the Wi-Fi setup code on its label.
        </Strip>
      )}
      {claimMsg ? <Strip tone="alarm" role="alert">{claimMsg}</Strip> : null}
      <Card>
        <SectionTitle>Where is the camera?</SectionTitle>
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Room">
          {['Living room', 'Hall', 'Bedroom', 'Kitchen', 'Shop', 'Other'].map((r) => (
            <button key={r} type="button" aria-pressed={room === r} onClick={() => setRoom(r)} className={cn('rounded-full border-[1.5px] px-3 py-1.5 text-[13.5px] font-semibold', room === r ? 'border-indigo bg-indigo-wash text-indigo' : 'border-line bg-paper')}>
              {r}
            </button>
          ))}
        </div>
        {room === 'Other' ? (
          <Field label="Room name" htmlFor="room-other" error={errors.room_label}>
            <TextInput id="room-other" value={otherRoom} onChange={(e) => setOtherRoom(e.target.value)} />
          </Field>
        ) : null}
        <p className="text-[12px] text-slate">Alerts name the room, so your contacts and the Desk know where to look.</p>
        <div className="grid grid-cols-2 gap-2">
          <Field label="Floor" htmlFor="floor">
            <Select id="floor" value={floor} onChange={(e) => setFloor(e.target.value)}>
              {['Ground', 'First', 'Second', 'Third or higher'].map((f) => (
                <option key={f}>{f}</option>
              ))}
            </Select>
          </Field>
          <Field label="Name of this device" htmlFor="dev-name" optional error={errors.name}>
            <TextInput id="dev-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={`${room === 'Other' ? otherRoom || 'Room' : room} unit`} />
          </Field>
        </div>
        <SectionTitle>When setup finishes, start in</SectionTitle>
        <RadioCards
          name="mode"
          value={mode}
          onChange={setMode}
          options={[
            { value: 'home', label: 'Home', sub: 'Someone SENTRIX doesn’t recognise shows as unusual activity after 10 seconds.' },
            { value: 'armed', label: 'Away', sub: 'The same after 3 seconds. Nobody is ever alerted for it.' },
          ]}
        />
      </Card>
      <Card>
        <SectionTitle>Check the picture</SectionTitle>
        {device.online ? (
          <img src={`/d/${device.id}/video`} alt="Live picture from the new device" className="aspect-video w-full rounded-xl bg-hatch object-cover" />
        ) : (
          <Strip tone="warn">No picture yet: the device is offline.</Strip>
        )}
        <p className="text-[12.5px] text-slate">Mount it 1.8 to 2.2 m high, tilted down, in a lit room. This check uses remote live view: video passes through the SENTRIX server and is not stored.</p>
        <Check id="view-ok" checked={viewChecked} onChange={setViewChecked} disabled={!device.online} label="The picture looks right" error={errors.view_checked} />
      </Card>
      <Button type="submit" variant="primary" busy={busy}>
        Save and continue
      </Button>
    </form>
  );
}

/* ------------------------------------------------------------ 6 */
function StepAlerts({ onDone }: { onDone: () => void }) {
  const q = useStep<any>(6);
  const qc = useQueryClient();
  const toast = useToast();
  const [v, setV] = useState<any>(null);
  const [pins, setPins] = useState({ sos_pin: '', sos_pin_confirm: '', duress_pin: '', duress_pin_confirm: '' });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [telegram, setTelegram] = useState<{ code: string; link: string | null } | null>(null);
  useEffect(() => {
    if (q.data && !v) setV({ window: q.data.values.window ?? q.data.window_choices?.[0] ?? 30, safety_defaults: q.data.values.safety_defaults ?? true, safety_off_ack: false, channels: { ...q.data.values.channels } });
  }, [q.data, v]);
  if (!v || !q.data) return <Spinner />;
  const d = q.data;

  async function linkTelegram() {
    try {
      const r = await api.post<{ code: string; link: string | null }>('/api/v2/telegram/link-code', {});
      setTelegram(r);
      if (r.link) window.open(r.link, '_blank', 'noopener');
    } catch (err) {
      toast(err instanceof ApiError ? err.message : 'Telegram linking is not available.');
    }
  }
  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    setMessage(undefined);
    try {
      await api.put('/api/v2/onboarding/6', { ...v, ...Object.fromEntries(Object.entries(pins).filter(([, x]) => x !== '')) });
      await qc.invalidateQueries({ queryKey: ['wizard'] });
      onDone();
    } catch (err) {
      const r = errorsOf(err);
      setErrors(r.fields);
      setMessage(r.message);
    } finally {
      setBusy(false);
    }
  }
  const pin = (k: keyof typeof pins, label: string, optional?: boolean) => (
    <Field label={label} htmlFor={`f-${k}`} error={errors[k]} optional={optional}>
      <TextInput id={`f-${k}`} type="password" inputMode="numeric" maxLength={d.pins?.pin_length ?? 6} autoComplete="off" value={pins[k]} invalid={Boolean(errors[k])} onChange={(e) => setPins({ ...pins, [k]: e.target.value.replace(/\D/g, '') })} />
    </Field>
  );
  return (
    <form onSubmit={save} noValidate className="grid gap-4">
      <h1 className="text-[22px] font-bold">Alerts</h1>
      <p className="-mt-2 text-[13.5px] text-slate">When SENTRIX sees a possible weapon or fire it asks you “Do you need assistance?”. SENTRIX does not contact 112 itself: you choose who is alerted.</p>
      {message ? <Strip tone="alarm" role="alert">{message}</Strip> : null}
      <Card>
        <SectionTitle>How long to wait for your answer</SectionTitle>
        <RadioCards name="window" value={v.window} onChange={(x: number) => setV({ ...v, window: x })} options={(d.window_choices ?? [30, 45, 60]).map((m: number) => ({ value: m, label: `${m} minutes` }))} />
        <p className="text-[12px] text-slate">No answer is recorded as “Unanswered”, never as safe.</p>
      </Card>
      <Card>
        <SectionTitle>Safety net</SectionTitle>
        <Check
          id="safety"
          checked={v.safety_defaults}
          onChange={(x) => setV({ ...v, safety_defaults: x, safety_off_ack: false })}
          label={<b>On (recommended)</b>}
          sub="Your confirmed contacts are alerted even before you answer: at once when two different models agree, after 2 minutes for a fire, and if the device goes offline during an alert. One camera check of a weapon alerts only you."
        />
        {!v.safety_defaults ? <Check id="safety-ack" checked={v.safety_off_ack} onChange={(x) => setV({ ...v, safety_off_ack: x })} label="I understand that with the safety net off, my contacts hear nothing until I answer." error={errors.safety_off_ack} /> : null}
      </Card>
      <Card>
        <SectionTitle>How SENTRIX reaches you</SectionTitle>
        <Check id="ch-email" checked={v.channels.email} onChange={(x) => setV({ ...v, channels: { ...v.channels, email: x } })} label="E-mail" />
        <Check id="ch-tg" checked={v.channels.telegram} onChange={(x) => setV({ ...v, channels: { ...v.channels, telegram: x } })} label="Telegram" sub={d.telegram_linked ? 'Linked.' : d.bot_configured ? 'Not linked yet.' : 'Telegram is not set up on this Portal yet.'} />
        {d.bot_configured && !d.telegram_linked ? (
          <div className="grid gap-2">
            <Button size="sm" block={false} onClick={linkTelegram}>
              Link Telegram
            </Button>
            {telegram ? (
              <Strip tone="info">
                {telegram.link ? 'Telegram opened in a new tab. ' : ''}If it didn’t, send <b className="select-all font-mono">/start {telegram.code}</b> to the SENTRIX alerts bot. The code works once, for 24 hours.
              </Strip>
            ) : null}
          </div>
        ) : null}
        <Check id="ch-sms" checked={v.channels.sms} onChange={(x) => setV({ ...v, channels: { ...v.channels, sms: x } })} label="SMS from the device" sub="When it is offline, or you haven’t seen the alert for 10 minutes." />
      </Card>
      <Card>
        <SectionTitle>PINs</SectionTitle>
        <p className="text-[12.5px] text-slate">The SOS PIN marks a false alarm, so a stranger or a curious child can’t cancel an alert. If someone forces you to cancel, type the duress PIN instead: your screen shows “Marked as false alarm” while your contacts and the Desk are told to send help.</p>
        {d.pins?.pin_set ? <Strip tone="ok">Your SOS PIN is set. Fill these in only to change it.</Strip> : null}
        <div className="grid grid-cols-2 gap-2">
          {pin('sos_pin', 'SOS PIN', d.pins?.pin_set)}
          {pin('sos_pin_confirm', 'SOS PIN again', d.pins?.pin_set)}
        </div>
        {d.pins?.contacts_exist ? (
          <div className="grid grid-cols-2 gap-2">
            {pin('duress_pin', 'Duress PIN', true)}
            {pin('duress_pin_confirm', 'Duress PIN again', true)}
          </div>
        ) : (
          <p className="text-[12px] text-slate">Add an emergency contact to set a duress PIN.</p>
        )}
        <p className="text-[12px] text-slate">{d.pins?.pin_length ?? 6} digits. Repeated or sequential PINs are refused; the duress PIN must differ in at least {d.pins?.duress_digits ?? 2} digits.</p>
      </Card>
      <Button type="submit" variant="primary" busy={busy}>
        Save and continue
      </Button>
    </form>
  );
}

/* ------------------------------------------------------------ 7 */
function StepReview({ onDone }: { onDone: () => void }) {
  const q = useStep<any>(7);
  const qc = useQueryClient();
  const refresh = useRefreshSession();
  const [consents, setConsents] = useState<Record<string, boolean> | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string>();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (q.data && !consents) setConsents({ ...q.data.consents });
  }, [q.data, consents]);
  if (!consents || !q.data) return <Spinner />;
  const d = q.data;
  async function finish() {
    setBusy(true);
    setErrors({});
    setMessage(undefined);
    try {
      const body = Object.fromEntries(d.purposes.map((p: any) => [`consent_${p.key}`, Boolean(consents![p.key])]));
      await api.put('/api/v2/onboarding/7', body);
      await qc.invalidateQueries();
      await refresh();
      onDone();
    } catch (err) {
      const r = errorsOf(err);
      setErrors(r.fields);
      setMessage(r.message ?? Object.values(r.fields)[0]);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="grid gap-4">
      <h1 className="text-[22px] font-bold">Review</h1>
      <p className="-mt-2 text-[13.5px] text-slate">One choice per purpose. You can change any of them later in Privacy.</p>
      {message ? <Strip tone="alarm" role="alert">{message}</Strip> : null}
      <Card>
        {d.purposes.map((p: any) => (
          <Check key={p.key} id={`c-${p.key}`} checked={Boolean(consents[p.key])} disabled={p.key === 'account_sos' && consents[p.key]} onChange={(x) => setConsents({ ...consents, [p.key]: x })} label={<b>{p.title}</b>} sub={p.text} error={errors[`consent_${p.key}`]} />
        ))}
        <a href="/privacy/notice" target="_blank" rel="noopener" className="text-[13px] font-semibold text-indigo underline">
          Read the privacy notice (version {d.notice_version})
        </a>
      </Card>
      <Card>
        <details>
          <summary className="cursor-pointer text-[14px] font-semibold">What your contacts would get</summary>
          <pre className="mt-2 whitespace-pre-wrap font-mono text-[11.5px] text-slate">{d.preview?.full}</pre>
        </details>
        <p className="text-[13px]">
          {d.confirmed_contacts?.length
            ? `${d.confirmed_contacts.join(', ')} will get alerts.`
            : d.pending_contacts?.length
              ? `${d.pending_contacts.join(', ')} hasn’t confirmed yet. Until then, alerts reach only you.`
              : 'No emergency contact yet: alerts reach only you.'}
        </p>
      </Card>
      <Button variant="primary" busy={busy} onClick={finish}>
        Finish setup
      </Button>
    </div>
  );
}

/* ------------------------------------------------------------ done */
function SetupDone() {
  const navigate = useNavigate();
  const { data: s } = useSession();
  const home = useQuery({ queryKey: ['home'], queryFn: () => api.get<any>('/api/v2/home') });
  const device = home.data?.devices?.[0];
  const protectedNow = Boolean(device?.claimed && device?.online);
  const text = useMemo(() => (protectedNow ? `The ${device?.room ?? ''} camera is watching.` : 'The device isn’t added and online yet, so nothing is watching. Finish the items below.'), [protectedNow, device?.room]);
  if (s?.household && !s.household.setupDone) return <Navigate to="/setup" replace />;
  return (
    <div className="mx-auto grid w-full max-w-xl gap-4 px-4 py-6 safe-x">
      <h1 className="text-[28px] font-bold">{protectedNow ? 'Your home is protected' : 'Almost there'}</h1>
      <p className="text-[14px] text-slate">{text}</p>
      <SetupChecklistInline />
      <Button variant="primary" onClick={() => navigate('/home')}>
        Go to Home
      </Button>
    </div>
  );
}

import { SetupChecklist as SetupChecklistInline } from '../owner/SetupChecklist';
