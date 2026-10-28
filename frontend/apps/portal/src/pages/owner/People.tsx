import { ApiError } from '@sentrix/api';
import { Button, EmptyState, ErrorState, Field, List, ListRow, Pill, Select, Sheet, Spinner, Strip, TextInput, useToast } from '@sentrix/ui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { api, useSession } from '../../lib/api';
import { CONTACT_STATUS, ContactForm, ContactRows, type ContactOut, type ContactSaved } from './contacts';

interface MemberOut {
  id: string;
  name: string;
  relation: string | null;
  age_band: string | null;
  invited: boolean;
}
interface MembersOut {
  editable: boolean;
  members: MemberOut[];
  max_members: number;
  age_bands: string[];
}
interface ContactsOut {
  editable: boolean;
  contacts: ContactOut[];
  max_contacts: number;
  can_add: boolean;
  declined_days: number | null;
}

const AGE: Record<string, string> = { adult: 'Adult', senior: 'Senior', minor: 'Child' };

/** People: who lives here (and may use the app) and who SENTRIX may alert. */
export function People() {
  const { data: s } = useSession();
  const qc = useQueryClient();
  const toast = useToast();
  const members = useQuery({ queryKey: ['members'], queryFn: () => api.get<MembersOut>('/api/v2/household/members') });
  const contacts = useQuery({ queryKey: ['contacts'], queryFn: () => api.get<ContactsOut>('/api/v2/household/contacts') });
  const [adding, setAdding] = useState(false);
  const [open, setOpen] = useState<ContactOut | null>(null);
  const [editing, setEditing] = useState(false);
  const [share, setShare] = useState<{ name: string; link: string | null; emailed: boolean } | null>(null);
  const [addMember, setAddMember] = useState(false);
  const owner = s?.role === 'owner';

  if (members.isLoading || contacts.isLoading) return <Spinner />;
  if (!members.data || !contacts.data) return <div className="p-4"><ErrorState message="Could not load people." onRetry={() => { members.refetch(); contacts.refetch(); }} /></div>;
  const refresh = () => Promise.all([qc.invalidateQueries({ queryKey: ['contacts'] }), qc.invalidateQueries({ queryKey: ['members'] })]);

  function saved(r: ContactSaved) {
    setAdding(false);
    setEditing(false);
    setOpen(null);
    setShare({ name: r.contact.name, link: r.share_link, emailed: r.emailed });
    refresh();
  }
  async function resend(c: ContactOut) {
    try {
      const r = await api.post<ContactSaved>(`/api/v2/household/contacts/${c.id}/resend`, {});
      setOpen(null);
      setShare({ name: c.name, link: r.share_link, emailed: r.emailed });
      refresh();
    } catch (err) {
      toast(err instanceof ApiError ? err.message : 'That did not work.');
    }
  }
  async function remove(c: ContactOut) {
    try {
      await api.del(`/api/v2/household/contacts/${c.id}`);
      setOpen(null);
      toast(`${c.name} removed. They get no more alerts.`);
      refresh();
    } catch (err) {
      toast(err instanceof ApiError ? err.message : 'That did not work.');
    }
  }
  async function removeMember(m: MemberOut) {
    try {
      await api.del(`/api/v2/household/members/${m.id}`);
      toast(`${m.name} removed.`);
      refresh();
    } catch (err) {
      toast(err instanceof ApiError ? err.message : 'That did not work.');
    }
  }

  const c = contacts.data;
  const m = members.data;
  return (
    <div className="grid gap-6 px-4 py-4 safe-x md:px-6">
      <h1 className="text-[22px] font-bold">People</h1>

      <section className="grid gap-2" aria-labelledby="ec">
        <h2 id="ec" className="text-[16px] font-bold">Emergency contacts</h2>
        <p className="-mt-1 max-w-prose text-[13px] text-slate">When you ask for help, or the safety net decides, SENTRIX alerts them. Each one confirms from a link before getting any alert.</p>
        {c.contacts.length ? <ContactRows contacts={c.contacts} onOpen={owner ? (x) => { setOpen(x); setEditing(false); } : undefined} /> : <EmptyState title="No emergency contacts yet">Until someone confirms, alerts reach only you.</EmptyState>}
        {owner && c.editable ? (
          adding ? (
            <ContactForm onSaved={saved} onCancel={() => setAdding(false)} />
          ) : c.can_add ? (
            <Button block={false} onClick={() => setAdding(true)}>
              Add an emergency contact
            </Button>
          ) : (
            <p className="text-[12.5px] text-slate">You have the most contacts allowed ({c.max_contacts}).</p>
          )
        ) : null}
      </section>

      <section className="grid gap-2" aria-labelledby="hh">
        <h2 id="hh" className="text-[16px] font-bold">Who lives here</h2>
        <List>
          {m.members.map((x) => (
            <ListRow
              key={x.id}
              label={x.name}
              sub={[x.relation, AGE[x.age_band ?? ''] ?? null, x.invited ? 'Uses the app' : null].filter(Boolean).join(', ')}
              value={owner && m.editable ? (
                <button type="button" className="font-semibold text-chilli underline" onClick={() => removeMember(x)}>
                  Remove
                </button>
              ) : undefined}
            />
          ))}
        </List>
        {!m.members.length ? <p className="text-[13px] text-slate">Only you so far.</p> : null}
        {owner && m.editable && m.members.length < m.max_members ? (
          addMember ? (
            <MemberForm ageBands={m.age_bands} onDone={() => { setAddMember(false); refresh(); }} onCancel={() => setAddMember(false)} />
          ) : (
            <Button block={false} onClick={() => setAddMember(true)}>
              Add someone who lives here
            </Button>
          )
        ) : null}
        <p className="text-[12px] text-slate">Faces are added on the device itself, by the device admin. Children’s faces are never added.</p>
      </section>

      <Sheet open={Boolean(open)} onClose={() => setOpen(null)} title={open?.name}>
        {open ? (
          <div className="grid gap-3">
            {editing ? (
              <ContactForm contact={open} onSaved={saved} onCancel={() => setEditing(false)} />
            ) : (
              <>
                <List>
                  <ListRow label="Status" value={<Pill tone={CONTACT_STATUS[open.status].tone}>{CONTACT_STATUS[open.status].text}</Pill>} />
                  <ListRow label="Mobile" value={open.phone_masked ?? 'Not set'} />
                  <ListRow label="E-mail" value={open.email ?? 'Not set'} />
                  <ListRow label="Telegram" value={open.telegram ? 'Linked' : 'Not linked'} />
                  <ListRow label="Within 30 minutes" value={open.can_reach_30min ? 'Yes' : 'No'} />
                </List>
                {open.unreachable_offline ? <Strip tone="warn">With no e-mail or Telegram, this contact can’t be reached while the Portal is offline.</Strip> : null}
                {open.status !== 'CONFIRMED' ? (
                  <Button variant="primary" onClick={() => resend(open)}>
                    Send the confirmation link again
                  </Button>
                ) : null}
                <Button onClick={() => setEditing(true)}>Edit</Button>
                <Button variant="quiet" onClick={() => remove(open)}>
                  Remove {open.name}
                </Button>
              </>
            )}
          </div>
        ) : null}
      </Sheet>

      <Sheet open={Boolean(share)} onClose={() => setShare(null)} title={share ? `Ask ${share.name} to confirm` : undefined}>
        {share ? <ShareLink {...share} onDone={() => setShare(null)} /> : null}
      </Sheet>
    </div>
  );
}

function ShareLink({ name, link, emailed, onDone }: { name: string; link: string | null; emailed: boolean; onDone: () => void }) {
  const toast = useToast();
  return (
    <div className="grid gap-3">
      <p className="text-[13.5px]">{emailed ? `We e-mailed ${name} the link.` : `Send ${name} this link.`} It works once and lets them confirm or decline.</p>
      {link ? (
        <>
          <TextInput readOnly value={link} aria-label="Confirmation link" onFocus={(e) => e.currentTarget.select()} />
          <Button
            variant="primary"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(link);
                toast('Link copied.');
              } catch {
                toast('Copy the link from the box.');
              }
            }}
          >
            Copy the link
          </Button>
          {'share' in navigator ? (
            <Button onClick={() => navigator.share({ title: 'SENTRIX emergency contact', text: `Please confirm that SENTRIX may alert you`, url: link }).catch(() => undefined)}>Share…</Button>
          ) : null}
        </>
      ) : (
        <Strip tone="info">This link was already shown once. Send it again to get a new one.</Strip>
      )}
      <Button variant="quiet" onClick={onDone}>
        Done
      </Button>
    </div>
  );
}

function MemberForm({ ageBands, onDone, onCancel }: { ageBands: string[]; onDone: () => void; onCancel: () => void }) {
  const toast = useToast();
  const [v, setV] = useState({ member_name: '', member_relation: '', age_band: ageBands[0] ?? 'adult', invite_email: '' });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    try {
      const r = await api.post<{ message: string }>('/api/v2/household/members', { ...v, invite_email: v.invite_email || null });
      toast(r.message);
      onDone();
    } catch (err) {
      if (err instanceof ApiError) {
        setErrors(Object.keys(err.fields).length ? err.fields : { member_name: err.message });
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={save} noValidate className="grid gap-3 rounded-xl border border-line bg-paper p-3">
      <Field label="Name" htmlFor="m-name" error={errors.member_name}>
        <TextInput id="m-name" value={v.member_name} onChange={(e) => setV({ ...v, member_name: e.target.value })} />
      </Field>
      <div className="grid grid-cols-2 gap-2">
        <Field label="Relation" htmlFor="m-rel" optional error={errors.member_relation}>
          <TextInput id="m-rel" value={v.member_relation} onChange={(e) => setV({ ...v, member_relation: e.target.value })} />
        </Field>
        <Field label="Age" htmlFor="m-age" error={errors.age_band}>
          <Select id="m-age" value={v.age_band} onChange={(e) => setV({ ...v, age_band: e.target.value })}>
            {ageBands.map((a) => (
              <option key={a} value={a}>
                {AGE[a] ?? a}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      {v.age_band !== 'minor' ? (
        <Field label="Invite them to the app" htmlFor="m-mail" optional error={errors.invite_email} help="They get their own sign-in and can answer alerts.">
          <TextInput id="m-mail" type="email" inputMode="email" value={v.invite_email} onChange={(e) => setV({ ...v, invite_email: e.target.value })} placeholder="Their e-mail" />
        </Field>
      ) : null}
      <div className="flex gap-2">
        <Button type="submit" variant="primary" size="sm" block={false} busy={busy}>
          Add
        </Button>
        <Button size="sm" variant="quiet" block={false} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
