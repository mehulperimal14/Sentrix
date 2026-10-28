import { ApiError } from '@sentrix/api';
import { Button, Check, Field, List, ListRow, Pill, Select, TextInput } from '@sentrix/ui';
import { useState, type FormEvent } from 'react';
import { api } from '../../lib/api';

/** ContactOut (sentrix_portal/routes/api_v2/household.py). */
export interface ContactOut {
  id: string;
  name: string;
  relation: string | null;
  priority: number | null;
  can_reach_30min: boolean;
  status: 'PENDING' | 'CONFIRMED' | 'DECLINED' | 'REVOKED';
  confirmed_at: string | null;
  phone_masked: string | null;
  email: string | null;
  telegram: boolean;
  unreachable_offline: boolean;
}

export interface ContactSaved {
  contact: ContactOut;
  share_link: string | null;
  emailed: boolean;
}

export const CONTACT_STATUS: Record<ContactOut['status'], { text: string; tone: 'ok' | 'warn' | 'alarm' | 'off' }> = {
  PENDING: { text: 'Waiting to confirm', tone: 'warn' },
  CONFIRMED: { text: 'Confirmed', tone: 'ok' },
  DECLINED: { text: 'Declined', tone: 'alarm' },
  REVOKED: { text: 'Opted out', tone: 'off' },
};

export function ContactRows({ contacts, onOpen }: { contacts: ContactOut[]; onOpen?: (c: ContactOut) => void }) {
  return (
    <List>
      {contacts.map((c) => {
        const s = CONTACT_STATUS[c.status];
        return (
          <ListRow
            key={c.id}
            label={c.name}
            sub={[c.relation, c.phone_masked, c.telegram ? 'Telegram linked' : null].filter(Boolean).join(', ')}
            value={<Pill tone={s.tone}>{s.text}</Pill>}
            onClick={onOpen ? () => onOpen(c) : undefined}
          />
        );
      })}
    </List>
  );
}

const RELATIONS = ['Family', 'Neighbour', 'Friend', 'Colleague', 'Other'];

/** Add or edit an emergency contact. The server checks the phone number,
 *  the e-mail and the priority and answers with field errors. */
export function ContactForm({ contact, onSaved, onCancel }: { contact?: ContactOut; onSaved: (r: ContactSaved) => void; onCancel: () => void }) {
  const [v, setV] = useState({
    name: contact?.name ?? '',
    relation: contact?.relation ?? 'Family',
    phone: '',
    email: contact?.email ?? '',
    can_reach_30min: contact?.can_reach_30min ?? true,
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string>();
  const [busy, setBusy] = useState(false);

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    setMessage(undefined);
    const body: Record<string, unknown> = { ...v, email: v.email || null };
    if (contact && !v.phone) delete body.phone;
    try {
      const r = contact ? await api.patch<ContactSaved>(`/api/v2/household/contacts/${contact.id}`, body) : await api.post<ContactSaved>('/api/v2/household/contacts', body);
      onSaved(r);
    } catch (err) {
      if (err instanceof ApiError) {
        setErrors(err.fields);
        if (!Object.keys(err.fields).length) setMessage(err.message);
      } else setMessage('That did not save. Try again.');
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={save} noValidate className="grid gap-3 rounded-xl border border-line p-3">
      {message ? <p role="alert" className="text-[13px] font-semibold text-chilli">{message}</p> : null}
      <Field label="Name" htmlFor="c-name" error={errors.name}>
        <TextInput id="c-name" autoComplete="off" value={v.name} invalid={Boolean(errors.name)} onChange={(e) => setV({ ...v, name: e.target.value })} />
      </Field>
      <div className="grid grid-cols-2 gap-2">
        <Field label="Who they are" htmlFor="c-rel" error={errors.relation}>
          <Select id="c-rel" value={RELATIONS.includes(v.relation) ? v.relation : 'Other'} onChange={(e) => setV({ ...v, relation: e.target.value })}>
            {RELATIONS.map((r) => (
              <option key={r}>{r}</option>
            ))}
          </Select>
        </Field>
        <Field label="Mobile number" htmlFor="c-phone" error={errors.phone} help={contact ? `Now ${contact.phone_masked ?? 'not set'}. Leave empty to keep it.` : undefined}>
          <TextInput id="c-phone" type="tel" inputMode="tel" autoComplete="off" value={v.phone} invalid={Boolean(errors.phone)} onChange={(e) => setV({ ...v, phone: e.target.value })} placeholder="+91 98xxx xxxxx" />
        </Field>
      </div>
      <Field label="E-mail" htmlFor="c-email" optional error={errors.email} help="We e-mail them the link to confirm.">
        <TextInput id="c-email" type="email" inputMode="email" autoComplete="off" value={v.email} invalid={Boolean(errors.email)} onChange={(e) => setV({ ...v, email: e.target.value })} />
      </Field>
      <Check id="c-30" checked={v.can_reach_30min} onChange={(x) => setV({ ...v, can_reach_30min: x })} label="Can reach the home within 30 minutes" />
      {errors.priority ? <p className="text-[12.5px] font-semibold text-chilli">{errors.priority}</p> : null}
      <div className="flex gap-2">
        <Button type="submit" variant="primary" size="sm" busy={busy} block={false}>
          {contact ? 'Save contact' : 'Add contact'}
        </Button>
        <Button size="sm" variant="quiet" block={false} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
