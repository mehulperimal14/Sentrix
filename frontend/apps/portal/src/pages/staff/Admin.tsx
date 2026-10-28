import { ApiError } from '@sentrix/api';
import { Button, EmptyState, ErrorState, Field, List, ListRow, Pill, QrCode, Select, Sheet, Spinner, Strip, TextInput, formatDayTime, useToast } from '@sentrix/ui';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent, type ReactNode } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import { api } from '../../lib/api';

/* The company admin console: customers, devices, staff and the audit log.
   Dense tables are fine here (laptops). Codes and the device identity are
   shown once, in a printable sheet. */

function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-line bg-paper">
      <table className="w-full min-w-[720px] text-left text-[13.5px]">
        <thead className="text-[12px] text-slate">
          <tr>
            {head.map((h) => (
              <th key={h} className="px-3 py-2 font-semibold">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
const td = 'border-t border-line px-3 py-2.5 align-top';
function errText(err: unknown) {
  return err instanceof ApiError ? err.message : 'That did not work.';
}
function useFieldErrors() {
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string>();
  return {
    errors,
    message,
    clear: () => {
      setErrors({});
      setMessage(undefined);
    },
    set: (err: unknown) => {
      if (err instanceof ApiError && Object.keys(err.fields).length) setErrors(err.fields);
      else setMessage(errText(err));
    },
  };
}

/* ------------------------------------------------------------ overview */
export function AdminOverview() {
  const q = useQuery({ queryKey: ['admin-overview'], queryFn: () => api.get<any>('/api/v2/admin/overview'), refetchInterval: 15_000 });
  if (q.isLoading) return <Spinner />;
  if (!q.data) return <ErrorState message="Could not load the overview." />;
  const d = q.data;
  const tiles = [
    { label: 'Customers', value: d.customers, to: '/admin/customers' },
    { label: 'Waiting to activate', value: d.pending, to: '/admin/customers' },
    { label: 'Devices claimed', value: d.claimed, to: '/admin/devices' },
    { label: 'Devices online', value: d.online, to: '/admin/devices' },
    { label: 'Staff', value: d.operators, to: '/admin/operators' },
  ];
  return (
    <div className="grid gap-5">
      <h1 className="text-[22px] font-bold">Overview</h1>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        {tiles.map((t) => (
          <Link key={t.label} to={t.to} className="rounded-xl border border-line bg-paper p-3 hover:bg-chalk">
            <span className="block text-[26px] font-bold tabular">{t.value}</span>
            <span className="text-[12.5px] text-slate">{t.label}</span>
          </Link>
        ))}
      </div>
      <section className="grid gap-2">
        <h2 className="text-[15px] font-bold">Devices by state</h2>
        <div className="flex flex-wrap gap-2">
          {Object.entries(d.device_states as Record<string, number>).map(([k, v]) => (
            <Pill key={k} tone="info">
              {k.toLowerCase()}: {v}
            </Pill>
          ))}
        </div>
      </section>
      <AuditChain chain={d.chain} />
    </div>
  );
}

function AuditChain({ chain }: { chain: { ok: boolean; entries: number; problems: number[] } }) {
  return chain.ok ? (
    <Strip tone="ok">The audit log is intact ({chain.entries} entries checked).</Strip>
  ) : (
    <Strip tone="alarm">The audit log chain is broken at {chain.problems.length} entries ({chain.problems.slice(0, 5).join(', ')}). Investigate before trusting it.</Strip>
  );
}

/* ------------------------------------------------------------ customers */
export function AdminCustomers() {
  const [search, setSearch] = useState('');
  const [adding, setAdding] = useState(false);
  const navigate = useNavigate();
  const q = useQuery({ queryKey: ['admin-customers', search], queryFn: () => api.get<any>(`/api/v2/admin/customers?q=${encodeURIComponent(search)}`) });
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-[22px] font-bold">Customers</h1>
        <div className="flex gap-2">
          <TextInput aria-label="Search customers" placeholder="Search name, code, city" value={search} onChange={(e) => setSearch(e.target.value)} className="w-64" />
          <Button variant="primary" block={false} onClick={() => setAdding(true)}>
            New customer
          </Button>
        </div>
      </div>
      {q.isLoading ? <Spinner /> : null}
      {q.data && q.data.rows.length === 0 ? <EmptyState title="No customers">Create one to print a welcome letter.</EmptyState> : null}
      {q.data?.rows.length ? (
        <Table head={['Code', 'Name', 'City', 'Owner', 'Setup', 'Devices', 'Status']}>
          {q.data.rows.map((r: any) => (
            <tr key={r.id} className="cursor-pointer hover:bg-chalk" onClick={() => navigate(`/admin/customers/${r.id}`)}>
              <td className={`${td} font-mono`}>{r.customer_code}</td>
              <td className={`${td} font-semibold`}>
                <Link to={`/admin/customers/${r.id}`}>{r.name ?? 'Unnamed'}</Link>
              </td>
              <td className={td}>{r.city}</td>
              <td className={td}>
                {r.owner_username}
                <small className="block text-slate">{r.owner_status?.toLowerCase()}</small>
              </td>
              <td className={td}>{r.setup_done ? 'Done' : `Step ${r.setup_step} of 7`}</td>
              <td className={`${td} tabular`}>{r.devices}</td>
              <td className={td}>
                <Pill tone={r.status === 'ACTIVE' ? 'ok' : 'off'}>{(r.status ?? '').toLowerCase()}</Pill>
              </td>
            </tr>
          ))}
        </Table>
      ) : null}
      <Sheet open={adding} onClose={() => setAdding(false)} title="New customer">
        <NewCustomer unassigned={q.data?.unassigned ?? []} onDone={(id) => navigate(`/admin/customers/${id}`)} />
      </Sheet>
    </div>
  );
}

function NewCustomer({ unassigned, onDone }: { unassigned: { id: string; serial: string }[]; onDone: (id: string) => void }) {
  const [v, setV] = useState({ name: '', username: '', email: '', phone: '', city: '', serial: '' });
  const e = useFieldErrors();
  const [busy, setBusy] = useState(false);
  async function save(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    e.clear();
    try {
      const r = await api.post<{ id: string }>('/api/v2/admin/customers', { ...v, serial: v.serial || null, email: v.email || null, phone: v.phone || null });
      onDone(r.id);
    } catch (err) {
      e.set(err);
    } finally {
      setBusy(false);
    }
  }
  const f = (k: keyof typeof v, label: string, opt?: boolean) => (
    <Field label={label} htmlFor={`nc-${k}`} error={e.errors[k]} optional={opt}>
      <TextInput id={`nc-${k}`} value={v[k]} onChange={(x) => setV({ ...v, [k]: x.target.value })} />
    </Field>
  );
  return (
    <form onSubmit={save} noValidate className="grid gap-3">
      {e.message ? <Strip tone="alarm">{e.message}</Strip> : null}
      {f('name', 'Customer name')}
      {f('username', 'Username for the owner')}
      {f('email', 'E-mail', true)}
      {f('phone', 'Mobile number', true)}
      {f('city', 'City')}
      <Field label="Assign a device" htmlFor="nc-serial" optional error={e.errors.serial}>
        <Select id="nc-serial" value={v.serial} onChange={(x) => setV({ ...v, serial: x.target.value })}>
          <option value="">Not now</option>
          {unassigned.map((d) => (
            <option key={d.id} value={d.serial}>
              {d.serial}
            </option>
          ))}
        </Select>
      </Field>
      <Button type="submit" variant="primary" busy={busy}>
        Create customer
      </Button>
    </form>
  );
}

export function AdminCustomer() {
  const { id } = useParams();
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: ['admin-customer', id], queryFn: () => api.get<any>(`/api/v2/admin/customers/${id}`) });
  const [serial, setSerial] = useState('');
  const [letter, setLetter] = useState<any>(null);
  const [busy, setBusy] = useState<string | null>(null);
  if (q.isLoading) return <Spinner />;
  if (!q.data) return <ErrorState message="Customer not found." />;
  const c = q.data;
  async function run(key: string, fn: () => Promise<any>) {
    setBusy(key);
    try {
      const r = await fn();
      if (r?.message) toast(r.message);
      await qc.invalidateQueries({ queryKey: ['admin-customer', id] });
      return r;
    } catch (err) {
      toast(errText(err));
    } finally {
      setBusy(null);
    }
  }
  return (
    <div className="grid gap-4">
      <Link to="/admin/customers" className="justify-self-start text-[13px] font-semibold text-slate hover:text-graphite">
        All customers
      </Link>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-[22px] font-bold">{c.name ?? 'Customer'}</h1>
        <span className="font-mono text-[13px] text-slate">{c.customer_code}</span>
        <Pill tone={c.status === 'ACTIVE' ? 'ok' : 'off'}>{(c.status ?? '').toLowerCase()}</Pill>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <section className="grid content-start gap-3 rounded-2xl bg-paper p-4">
          <h2 className="text-[15px] font-bold">Owner</h2>
          {c.owner ? (
            <List>
              <ListRow label="Username" value={c.owner.username} />
              <ListRow label="Name" value={c.owner.display_name ?? ''} />
              <ListRow label="Account" value={(c.owner.status ?? '').toLowerCase()} />
              <ListRow label="E-mail" value={c.owner.email_masked ?? 'Not set'} />
              <ListRow label="Mobile" value={c.owner.phone_masked ?? 'Not set'} />
              <ListRow label="Last sign-in" value={c.owner.last_login_at ? formatDayTime(c.owner.last_login_at) : 'Never'} />
              <ListRow label="Setup" value={c.setup_done ? 'Done' : `Step ${c.setup_step} of 7`} />
            </List>
          ) : (
            <p className="text-[13px] text-slate">No owner account.</p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" size="sm" block={false} busy={busy === 'letter'} onClick={async () => setLetter(await run('letter', () => api.post('/api/v2/admin/customers/' + id + '/letter', { new_claim_codes: false })))}>
              Print welcome letter
            </Button>
            <Button size="sm" block={false} busy={busy === 'resend'} onClick={() => run('resend', () => api.post(`/api/v2/admin/customers/${id}/resend`, {}))}>
              E-mail a new activation code
            </Button>
            <Button size="sm" variant="quiet" block={false} busy={busy === 'status'} onClick={() => run('status', () => api.post(`/api/v2/admin/customers/${id}/status`, { action: c.status === 'ACTIVE' ? 'deactivate' : 'reactivate' }))}>
              {c.status === 'ACTIVE' ? 'Deactivate' : 'Reactivate'}
            </Button>
          </div>
          <p className="text-[12px] text-slate">Printing a letter makes a new activation code; the old one stops working.</p>
        </section>
        <section className="grid content-start gap-3 rounded-2xl bg-paper p-4">
          <h2 className="text-[15px] font-bold">Devices</h2>
          {c.devices.length ? (
            <List>
              {c.devices.map((d: any) => (
                <ListRow key={d.id} label={d.serial} sub={d.name} value={`${d.state.toLowerCase()}${d.online ? ', online' : ''}`} />
              ))}
            </List>
          ) : (
            <p className="text-[13px] text-slate">No device assigned.</p>
          )}
          {c.unassigned.length ? (
            <div className="flex gap-2">
              <Select aria-label="Device to assign" value={serial} onChange={(e) => setSerial(e.target.value)}>
                <option value="">Choose a device…</option>
                {c.unassigned.map((d: any) => (
                  <option key={d.id} value={d.serial}>
                    {d.serial}
                  </option>
                ))}
              </Select>
              <Button size="sm" block={false} disabled={!serial} busy={busy === 'assign'} onClick={() => run('assign', () => api.post(`/api/v2/admin/customers/${id}/assign`, { serial }))}>
                Assign
              </Button>
            </div>
          ) : null}
          <h2 className="pt-2 text-[15px] font-bold">Account history</h2>
          <List>
            {c.events.slice(0, 12).map((e: any) => (
              <ListRow key={e.id} label={e.event} sub={e.actor_type} value={e.at ? formatDayTime(e.at) : ''} />
            ))}
          </List>
        </section>
      </div>
      <Sheet open={Boolean(letter)} onClose={() => setLetter(null)} title="Welcome letter">
        {letter ? <WelcomeLetter letter={letter} /> : null}
      </Sheet>
    </div>
  );
}

function WelcomeLetter({ letter: l }: { letter: any }) {
  return (
    <div className="grid gap-4">
      <div className="printable grid gap-4 rounded-xl border border-line bg-white p-5 text-black">
        <h2 className="text-[20px] font-bold">Welcome to SENTRIX</h2>
        <p className="text-[14px]">Dear {l.owner_name ?? l.customer_name}, your account is ready. Open the link below (or scan the code) and choose a password.</p>
        <div className="flex flex-wrap items-center gap-5">
          <QrCode value={l.activation_url} size={132} label="Activation link" />
          <div className="grid gap-1 text-[14px]">
            <span>
              Username: <b className="font-mono">{l.username}</b>
            </span>
            <span>
              Activation code: <b className="font-mono">{l.activation_code}</b>
            </span>
            <span className="text-[12.5px]">Valid for {l.ttl_hours} hours, once. Then sign in at {l.portal_url}</span>
          </div>
        </div>
        {l.devices.map((d: any) => (
          <div key={d.serial} className="flex flex-wrap items-center gap-5 border-t border-line pt-4">
            <QrCode value={d.claim_url} size={110} label={`Add device ${d.serial}`} />
            <div className="grid gap-1 text-[14px]">
              <span>
                Device: <b className="font-mono">{d.serial}</b>
              </span>
              {d.claim_code ? (
                <span>
                  Device code: <b className="font-mono">{d.claim_code}</b>
                </span>
              ) : (
                <span className="text-[12.5px]">The device code is on the label of the device.</span>
              )}
            </div>
          </div>
        ))}
      </div>
      <Button variant="primary" onClick={() => window.print()}>
        Print
      </Button>
      <p className="text-[12px] text-slate">These codes are shown only now.</p>
    </div>
  );
}

/* ------------------------------------------------------------ devices */
export function AdminDevices() {
  const qc = useQueryClient();
  const toast = useToast();
  const [state, setState] = useState('');
  const q = useQuery({ queryKey: ['admin-devices', state], queryFn: () => api.get<any>(`/api/v2/admin/devices?state=${state}`), refetchInterval: 15_000 });
  const [label, setLabel] = useState<any>(null);
  const [open, setOpen] = useState<any>(null);
  const [provisioning, setProvisioning] = useState(false);
  async function provision(ev: FormEvent) {
    ev.preventDefault();
    setProvisioning(true);
    try {
      setLabel(await api.post('/api/v2/admin/devices', { serial: q.data?.next_serial }));
      qc.invalidateQueries({ queryKey: ['admin-devices'] });
    } catch (err) {
      toast(errText(err));
    } finally {
      setProvisioning(false);
    }
  }
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-[22px] font-bold">Devices</h1>
        <form onSubmit={provision} className="flex gap-2">
          <Select aria-label="Show devices" value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">All states</option>
            {['UNASSIGNED', 'ASSIGNED', 'CLAIMED', 'RETIRED'].map((s) => (
              <option key={s} value={s}>
                {s.toLowerCase()}
              </option>
            ))}
          </Select>
          <Button type="submit" variant="primary" block={false} busy={provisioning}>
            New device {q.data?.next_serial ? `(${q.data.next_serial})` : ''}
          </Button>
        </form>
      </div>
      {q.isLoading ? <Spinner /> : null}
      {q.data?.rows.length ? (
        <Table head={['Serial', 'Name', 'State', 'Customer', 'Online', 'AI', 'Last seen']}>
          {q.data.rows.map((d: any) => (
            <tr key={d.id} className="cursor-pointer hover:bg-chalk" onClick={() => setOpen(d)}>
              <td className={`${td} font-mono font-semibold`}>{d.serial}</td>
              <td className={td}>{d.name}</td>
              <td className={td}>{d.state.toLowerCase()}</td>
              <td className={`${td} font-mono`}>{d.customer_code}</td>
              <td className={td}>{d.online ? <Pill tone="ok">online</Pill> : <Pill tone="off">offline</Pill>}</td>
              <td className={`${td} tabular`}>{d.ai_hz != null ? `${d.ai_hz.toFixed(1)} Hz` : ''}</td>
              <td className={`${td} tabular`}>{d.last_seen_at ? formatDayTime(d.last_seen_at) : 'Never'}{d.profile_behind ? <small className="block text-haldi">profile behind</small> : null}</td>
            </tr>
          ))}
        </Table>
      ) : q.data ? (
        <EmptyState title="No devices">Provision one to print its label.</EmptyState>
      ) : null}
      <Sheet open={Boolean(open)} onClose={() => setOpen(null)} title={open?.serial}>
        {open ? <DeviceActions device={open} onLabel={(l) => { setOpen(null); setLabel(l); }} onDone={() => { setOpen(null); qc.invalidateQueries({ queryKey: ['admin-devices'] }); }} /> : null}
      </Sheet>
      <Sheet open={Boolean(label)} onClose={() => setLabel(null)} title="Device label">
        {label ? <DeviceLabel label={label} /> : null}
      </Sheet>
    </div>
  );
}

function DeviceActions({ device: d, onLabel, onDone }: { device: any; onLabel: (l: any) => void; onDone: () => void }) {
  const toast = useToast();
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<'transfer' | 'retire' | null>(null);
  async function run<T>(key: string, fn: () => Promise<T>): Promise<T | undefined> {
    setBusy(key);
    try {
      return await fn();
    } catch (err) {
      toast(errText(err));
      return undefined;
    } finally {
      setBusy(null);
    }
  }
  const reprintable = d.state === 'UNASSIGNED' || d.state === 'ASSIGNED';
  return (
    <div className="grid gap-3">
      <List>
        <ListRow label="State" value={d.state.toLowerCase()} />
        <ListRow label="Customer" value={d.customer_code ?? 'None'} />
        <ListRow label="Firmware" value={d.firmware ?? 'Unknown'} />
      </List>
      {reprintable ? (
        <Button busy={busy === 'label'} onClick={async () => { const l = await run('label', () => api.post(`/api/v2/admin/devices/${d.id}/label`, {})); if (l) onLabel(l); }} sub="A new device code; the old one stops working.">
          Print a new label
        </Button>
      ) : null}
      {d.state === 'UNASSIGNED' ? (
        <div className="flex gap-2">
          <TextInput aria-label="Customer code" placeholder="Customer code" value={code} onChange={(e) => setCode(e.target.value)} />
          <Button block={false} disabled={!code} busy={busy === 'assign'} onClick={async () => { if (await run('assign', () => api.post(`/api/v2/admin/devices/${d.id}/assign`, { customer_code: code }))) onDone(); }}>
            Assign
          </Button>
        </div>
      ) : null}
      {d.state !== 'RETIRED' ? (
        confirm ? (
          <Strip tone="alarm">
            <p className="mb-2">{confirm === 'transfer' ? 'The device leaves its household (unpaired first) and gets a new device code.' : 'The device stops working with SENTRIX for good.'}</p>
            <Button variant="alarm" size="sm" block={false} busy={busy === confirm} onClick={async () => {
              const r = await run(confirm, () => api.post<any>(`/api/v2/admin/devices/${d.id}/${confirm}`, {}));
              if (r && confirm === 'transfer') onLabel(r);
              else if (r) { toast(r.message ?? 'Retired.'); onDone(); }
            }}>
              {confirm === 'transfer' ? 'Transfer it' : 'Retire it'}
            </Button>
          </Strip>
        ) : (
          <div className="flex gap-2">
            <Button size="sm" variant="quiet" block={false} onClick={() => setConfirm('transfer')}>
              Transfer…
            </Button>
            <Button size="sm" variant="quiet" block={false} onClick={() => setConfirm('retire')}>
              Retire…
            </Button>
          </div>
        )
      ) : null}
    </div>
  );
}

function DeviceLabel({ label: l }: { label: any }) {
  function downloadIdentity() {
    const blob = new Blob([JSON.stringify(l.identity, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    Object.assign(document.createElement('a'), { href: url, download: 'identity.json' }).click();
    URL.revokeObjectURL(url);
  }
  return (
    <div className="grid gap-4">
      <div className="printable flex flex-wrap items-center gap-5 rounded-xl border border-line bg-white p-5 text-black">
        <QrCode value={l.claim_url} size={132} label={`Add device ${l.serial}`} />
        <div className="grid gap-1 text-[14px]">
          <b className="text-[16px]">{l.title}</b>
          <span>
            Serial: <b className="font-mono">{l.serial}</b>
          </span>
          {l.claim_code ? (
            <span>
              Device code: <b className="font-mono">{l.claim_code}</b>
            </span>
          ) : null}
          {l.setup_code ? (
            <span>
              Wi-Fi setup code: <b className="font-mono">{l.setup_code}</b>
            </span>
          ) : null}
        </div>
      </div>
      <Button variant="primary" onClick={() => window.print()}>
        Print the label
      </Button>
      {l.identity ? (
        <>
          <Button onClick={downloadIdentity} sub="Copy it to the device as /etc/sentrix/identity.json. It holds the device’s secret and is shown only now.">
            Download identity.json
          </Button>
        </>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------ staff */
export function AdminStaff() {
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: ['admin-staff'], queryFn: () => api.get<any>('/api/v2/admin/operators') });
  const [adding, setAdding] = useState(false);
  const [code, setCode] = useState<any>(null);
  async function act(id: string, action: string) {
    try {
      await api.post(`/api/v2/admin/operators/${id}`, { action });
      toast('Saved.');
      qc.invalidateQueries({ queryKey: ['admin-staff'] });
    } catch (err) {
      toast(errText(err));
    }
  }
  if (q.isLoading) return <Spinner />;
  if (!q.data) return <ErrorState message="Could not load staff." />;
  return (
    <div className="grid gap-4">
      <div className="flex items-end justify-between gap-3">
        <h1 className="text-[22px] font-bold">Staff</h1>
        <Button variant="primary" block={false} onClick={() => setAdding(true)}>
          New staff account
        </Button>
      </div>
      <Table head={['Username', 'Name', 'Role', 'Status', 'Two-factor', 'Last sign-in', '']}>
        {q.data.staff.map((u: any) => (
          <tr key={u.id}>
            <td className={`${td} font-semibold`}>{u.username}{u.me ? <small className="block font-normal text-slate">you</small> : null}</td>
            <td className={td}>{u.display_name}</td>
            <td className={td}>{u.role === 'operator' ? 'Desk operator' : 'Admin'}</td>
            <td className={td}>{(u.status ?? '').toLowerCase()}</td>
            <td className={td}>{u.totp_enabled ? 'On' : 'Not set up'}</td>
            <td className={`${td} tabular`}>{u.last_login_at ? formatDayTime(u.last_login_at) : 'Never'}</td>
            <td className={td}>
              {!u.me ? (
                <div className="flex flex-wrap gap-2 text-[12.5px] font-semibold">
                  <button type="button" className="text-indigo underline" onClick={() => act(u.id, u.status === 'DISABLED' ? 'enable' : 'disable')}>
                    {u.status === 'DISABLED' ? 'Enable' : 'Disable'}
                  </button>
                  <button type="button" className="text-indigo underline" onClick={() => act(u.id, 'reset_2fa')}>
                    Reset two-factor
                  </button>
                  {u.status === 'PENDING' ? (
                    <button type="button" className="text-indigo underline" onClick={() => act(u.id, 'resend')}>
                      New code
                    </button>
                  ) : null}
                </div>
              ) : null}
            </td>
          </tr>
        ))}
      </Table>
      <Sheet open={adding} onClose={() => setAdding(false)} title="New staff account">
        <NewStaff roles={q.data.roles} onDone={(c) => { setAdding(false); setCode(c); qc.invalidateQueries({ queryKey: ['admin-staff'] }); }} />
      </Sheet>
      <Sheet open={Boolean(code)} onClose={() => setCode(null)} title="Activation code">
        {code ? (
          <div className="grid gap-3">
            <p className="text-[13.5px]">Give this to {code.user.display_name ?? code.user.username} in person. It works once, for {code.ttl_hours} hours; they set up two-factor at the first sign-in.</p>
            <QrCode value={code.activation_url} size={140} label="Activation link" />
            <p className="font-mono text-[18px] font-bold">{code.activation_code}</p>
          </div>
        ) : null}
      </Sheet>
    </div>
  );
}

function NewStaff({ roles, onDone }: { roles: string[]; onDone: (c: any) => void }) {
  const [v, setV] = useState({ role: 'operator', username: '', display_name: '', email: '' });
  const e = useFieldErrors();
  const [busy, setBusy] = useState(false);
  async function save(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    e.clear();
    try {
      onDone(await api.post('/api/v2/admin/operators', { ...v, email: v.email || null }));
    } catch (err) {
      e.set(err);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={save} noValidate className="grid gap-3">
      {e.message ? <Strip tone="alarm">{e.message}</Strip> : null}
      <Field label="Role" htmlFor="ns-role">
        <Select id="ns-role" value={v.role} onChange={(x) => setV({ ...v, role: x.target.value })}>
          {roles.map((r) => (
            <option key={r} value={r}>
              {r === 'operator' ? 'Desk operator' : 'Admin'}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="Username" htmlFor="ns-user" error={e.errors.username}>
        <TextInput id="ns-user" value={v.username} onChange={(x) => setV({ ...v, username: x.target.value })} />
      </Field>
      <Field label="Name" htmlFor="ns-name" error={e.errors.display_name}>
        <TextInput id="ns-name" value={v.display_name} onChange={(x) => setV({ ...v, display_name: x.target.value })} />
      </Field>
      <Field label="E-mail" htmlFor="ns-mail" optional error={e.errors.email}>
        <TextInput id="ns-mail" type="email" value={v.email} onChange={(x) => setV({ ...v, email: x.target.value })} />
      </Field>
      <Button type="submit" variant="primary" busy={busy}>
        Create
      </Button>
    </form>
  );
}

/* ------------------------------------------------------------ audit */
export function AdminAudit() {
  const q = useInfiniteQuery({
    queryKey: ['admin-audit'],
    queryFn: ({ pageParam }) => api.get<any>(`/api/v2/admin/audit${pageParam ? `?cursor=${encodeURIComponent(pageParam)}` : ''}`),
    initialPageParam: '' as string,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  if (q.isLoading) return <Spinner />;
  if (!q.data) return <ErrorState message="Could not load the audit log." />;
  const rows = q.data.pages.flatMap((p) => p.rows);
  return (
    <div className="grid gap-4">
      <h1 className="text-[22px] font-bold">Audit log</h1>
      <AuditChain chain={q.data.pages[0].chain} />
      <Table head={['When', 'Event', 'Who', 'What', 'Detail']}>
        {rows.map((r: any) => (
          <tr key={r.id}>
            <td className={`${td} whitespace-nowrap tabular`}>{r.at ? formatDayTime(r.at) : ''}</td>
            <td className={`${td} font-mono text-[12.5px]`}>{r.event}</td>
            <td className={td}>{r.actor_type}{r.actor_id ? <small className="block font-mono text-slate">{String(r.actor_id).slice(0, 10)}</small> : null}</td>
            <td className={td}>{r.target_type}{r.target_id ? <small className="block font-mono text-slate">{String(r.target_id).slice(0, 10)}</small> : null}</td>
            <td className={`${td} max-w-[360px] break-words font-mono text-[11.5px] text-slate`}>{Object.keys(r.detail ?? {}).length ? JSON.stringify(r.detail) : ''}</td>
          </tr>
        ))}
      </Table>
      {q.hasNextPage ? (
        <Button block={false} busy={q.isFetchingNextPage} onClick={() => q.fetchNextPage()}>
          Show older entries
        </Button>
      ) : null}
    </div>
  );
}
