import { Wordmark, cn } from '@sentrix/ui';
import { lazy, Suspense } from 'react';
import { Link, NavLink, Navigate, Route, Routes } from 'react-router';
import { Spinner } from '@sentrix/ui';
import { api, useSession } from '../../lib/api';

const Diagnostics = lazy(() => import('./Diagnostics'));
const Recordings = lazy(() => import('./Recordings'));
const Hardware = lazy(() => import('./Hardware'));
const ActivityPage = lazy(() => import('./Activity'));
const Faces = lazy(() => import('./Faces'));
const Network = lazy(() => import('./Network'));
const Sos = lazy(() => import('./Sos'));

const NAV = [
  { to: 'diagnostics', label: 'Diagnostics' },
  { to: 'activity', label: 'Activity' },
  { to: 'sos', label: 'Help requests' },
  { to: 'recordings', label: 'Record video' },
  { to: 'faces', label: 'Faces' },
  { to: 'hardware', label: 'Hardware' },
  { to: 'network', label: 'Network' },
];

/** Device admin, on the home network with the device password only. */
export default function AdminApp() {
  const { data } = useSession();
  async function signOut() {
    try {
      await api.post('/api/logout', {});
    } finally {
      location.assign('/login');
    }
  }
  return (
    <div className="flex min-h-dvh flex-col bg-chalk">
      {data?.testMode ? (
        <div className="bg-haldi-wash px-3 py-1 text-center text-[11.5px] font-bold text-haldi">Test mode: messages reach only the test list, and SENTRIX never contacts 112.</div>
      ) : null}
      <header className="border-b border-line bg-paper">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-2.5">
          <div className="flex items-center gap-3">
            <Wordmark />
            <span className="text-[13px] font-semibold text-slate">Device admin{data?.device.serial ? `, ${data.device.serial}` : ''}</span>
          </div>
          <div className="flex items-center gap-3 text-[13.5px] font-semibold">
            <Link to="/" className="text-indigo underline">
              Device screen
            </Link>
            <button type="button" onClick={signOut} className="text-slate underline">
              Sign out
            </button>
          </div>
        </div>
        <nav aria-label="Device admin" className="mx-auto flex max-w-7xl gap-1 overflow-x-auto px-4 pb-2">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} className={({ isActive }) => cn('whitespace-nowrap rounded-lg px-3 py-1.5 text-[13.5px] font-semibold', isActive ? 'bg-chalk text-graphite' : 'text-slate hover:text-graphite')}>
              {n.label}
            </NavLink>
          ))}
        </nav>
      </header>
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-5">
        <Suspense fallback={<Spinner />}>
          <Routes>
            <Route index element={<Navigate to="diagnostics" replace />} />
            <Route path="diagnostics" element={<Diagnostics />} />
            <Route path="activity" element={<ActivityPage />} />
            <Route path="sos" element={<Sos />} />
            <Route path="recordings" element={<Recordings />} />
            <Route path="faces" element={<Faces />} />
            <Route path="hardware" element={<Hardware />} />
            <Route path="network" element={<Network />} />
            <Route path="*" element={<Navigate to="diagnostics" replace />} />
          </Routes>
        </Suspense>
      </main>
    </div>
  );
}
