import { List, ListRow } from '@sentrix/ui';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { api, useSession } from '../../lib/api';
import { useHome } from '../../lib/data';
import type { ContactOut } from './contacts';

interface Item {
  key: string;
  done: boolean;
  label: string;
  sub: string;
  to: string;
}

/** What still stands between the household and full protection. The compact
 *  form (Home) lists only what is left and disappears when nothing is. */
export function SetupChecklist({ compact }: { compact?: boolean }) {
  const navigate = useNavigate();
  const { data: s } = useSession();
  const owner = s?.role === 'owner';
  const home = useHome();
  const contacts = useQuery({ queryKey: ['contacts'], queryFn: () => api.get<{ contacts: ContactOut[] }>('/api/v2/household/contacts'), enabled: owner });
  const settings = useQuery({ queryKey: ['settings'], queryFn: () => api.get<any>('/api/v2/settings'), enabled: owner });
  if (!owner || !home.data || !contacts.data || !settings.data) return null;

  const devices = home.data.devices;
  const confirmed = contacts.data.contacts.filter((c) => c.status === 'CONFIRMED');
  const waiting = contacts.data.contacts.filter((c) => c.status === 'PENDING');
  const bot = settings.data.telegram?.bot_configured;
  const items: Item[] = [
    {
      key: 'device',
      done: devices.some((d) => d.claimed && d.online),
      label: devices.some((d) => d.claimed) ? 'Bring the device online' : 'Add your device',
      sub: devices.some((d) => d.claimed) ? 'It is added but not connected.' : 'Nothing is watching until it is added.',
      to: devices.some((d) => d.claimed) ? '/settings/devices' : '/setup/device',
    },
    {
      key: 'contact',
      done: confirmed.length > 0,
      label: waiting.length ? `${waiting[0].name} hasn’t confirmed yet` : 'Add an emergency contact',
      sub: 'Until a contact confirms, alerts reach only you.',
      to: '/people',
    },
    {
      key: 'pin',
      done: Boolean(settings.data.pins?.pin_set),
      label: 'Set your SOS PIN',
      sub: 'It marks a false alarm, so nobody else can cancel an alert.',
      to: '/settings/alerts',
    },
  ];
  if (bot) {
    items.push({
      key: 'telegram',
      done: (settings.data.telegram?.links?.length ?? 0) > 0,
      label: 'Link Telegram',
      sub: 'Answer alerts from Telegram without signing in.',
      to: '/settings/alerts',
    });
  }
  const left = items.filter((i) => !i.done);
  if (compact && left.length === 0) return null;
  const shown = compact ? left : items;
  return (
    <section aria-label="Finish setting up" className="grid gap-2">
      <h2 className="text-[13px] font-bold text-slate">{left.length ? `Finish setting up: ${left.length} left` : 'Everything is set up'}</h2>
      <List>
        {shown.map((i) => (
          <ListRow key={i.key} label={i.label} sub={i.done ? undefined : i.sub} value={i.done ? 'Done' : 'Open'} tone={i.done ? 'ok' : undefined} onClick={i.done ? undefined : () => navigate(i.to)} />
        ))}
      </List>
    </section>
  );
}
