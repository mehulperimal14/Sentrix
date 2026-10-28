import { Spinner, Wordmark, cn } from '@sentrix/ui';
import { Activity, Home, Settings, Users } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link, NavLink, Navigate, Outlet, useLocation } from 'react-router';
import { OpenAlertBar } from './features/alert/OpenAlertBar';
import { homeFor, useSession, type Session } from './lib/api';

export function TestBanner({ session }: { session?: Session }) {
  if (!session?.testMode) return null;
  return (
    <div className="bg-haldi-wash px-3 py-1 text-center text-[11.5px] font-bold text-haldi safe-x">
      SENTRIX demo. Test mode: messages reach only the test list, and SENTRIX never contacts 112.
    </div>
  );
}

/** Signed-in pages; `roles` limits who may see them, everyone else goes home. */
export function RequireRole({ roles, children }: { roles: Session['role'][]; children: ReactNode }) {
  const { data, isLoading, isError } = useSession();
  const location = useLocation();
  if (isLoading) return <Spinner />;
  if (isError || !data?.signedIn) return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />;
  if (!roles.includes(data.role)) return <Navigate to={homeFor(data)} replace />;
  return <>{children}</>;
}

const TABS = [
  { to: '/home', label: 'Home', icon: Home },
  { to: '/activity', label: 'Activity', icon: Activity },
  { to: '/people', label: 'People', icon: Users },
  { to: '/settings', label: 'Settings', icon: Settings },
];

/** The owner and member app: top bar on wide screens, tab bar on phones. */
export function OwnerShell() {
  const { data } = useSession();
  const location = useLocation();
  if (data?.role === 'owner' && data.household && !data.household.setupDone && !location.pathname.startsWith('/setup')) {
    return <Navigate to="/setup" replace />;
  }
  return (
    <div className="flex min-h-dvh flex-col bg-chalk">
      <TestBanner session={data} />
      <OpenAlertBar />
      <header className="sticky top-0 z-30 hidden border-b border-line bg-paper/95 backdrop-blur md:block">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-6 px-6 py-3">
          <Link to="/home" className="text-graphite">
            <Wordmark />
          </Link>
          <nav aria-label="Sections" className="flex gap-1">
            {TABS.map((t) => (
              <NavLink key={t.to} to={t.to} className={({ isActive }) => cn('rounded-lg px-3 py-1.5 text-[14px] font-semibold', isActive ? 'bg-chalk text-graphite' : 'text-slate hover:text-graphite')}>
                {t.label}
              </NavLink>
            ))}
          </nav>
          <Link to="/settings/account" className="text-[13.5px] font-semibold text-slate hover:text-graphite">
            {data?.user?.name}
          </Link>
        </div>
      </header>
      <main className="mx-auto w-full max-w-5xl flex-1 pb-24 md:pb-10">
        <Outlet />
      </main>
      <nav aria-label="Sections" className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-4 border-t border-line bg-paper safe-bottom md:hidden">
        {TABS.map((t) => (
          <NavLink key={t.to} to={t.to} className={({ isActive }) => cn('grid justify-items-center gap-0.5 pb-2 pt-1.5 text-[11.5px] font-semibold', isActive ? 'text-indigo' : 'text-slate')}>
            {({ isActive }) => (
              <>
                <i className={cn('mb-0.5 block h-[3px] w-5 rounded-full', isActive ? 'bg-indigo' : 'bg-transparent')} />
                <t.icon aria-hidden className="size-5" />
                {t.label}
              </>
            )}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}

/** Pages people reach without an account: sign-in, activation, contact pages. */
export function PublicShell({ wide }: { wide?: boolean }) {
  const { data } = useSession();
  return (
    <div className="flex min-h-dvh flex-col bg-chalk">
      <TestBanner session={data} />
      <header className="mx-auto flex w-full max-w-5xl items-center px-4 py-3 md:px-6">
        <Wordmark className="text-graphite" />
      </header>
      <main className={cn('mx-auto w-full flex-1 px-4 pb-12', wide ? 'max-w-3xl' : 'max-w-md')}>
        <Outlet />
      </main>
    </div>
  );
}

/** The Desk and the admin console (laptops, dense layouts allowed). */
export function StaffShell({ area }: { area: 'desk' | 'admin' }) {
  const { data } = useSession();
  const links =
    area === 'admin'
      ? [
          { to: '/admin', label: 'Overview', end: true },
          { to: '/admin/customers', label: 'Customers' },
          { to: '/admin/devices', label: 'Devices' },
          { to: '/admin/operators', label: 'Staff' },
          { to: '/admin/audit', label: 'Audit log' },
        ]
      : [{ to: '/desk', label: 'Requests', end: true }];
  return (
    <div className="flex min-h-dvh flex-col bg-chalk">
      <TestBanner session={data} />
      <header className="border-b border-line bg-paper">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-2.5 md:px-6">
          <div className="flex items-center gap-4">
            <Wordmark />
            <span className="text-[13px] font-semibold text-slate">{area === 'desk' ? 'Desk (simulated responder console)' : 'Admin'}</span>
          </div>
          <nav aria-label="Sections" className="flex flex-wrap gap-1">
            {links.map((l) => (
              <NavLink key={l.to} to={l.to} end={l.end} className={({ isActive }) => cn('rounded-lg px-3 py-1.5 text-[13.5px] font-semibold', isActive ? 'bg-chalk text-graphite' : 'text-slate hover:text-graphite')}>
                {l.label}
              </NavLink>
            ))}
            <NavLink to="/account" className={({ isActive }) => cn('rounded-lg px-3 py-1.5 text-[13.5px] font-semibold', isActive ? 'bg-chalk text-graphite' : 'text-slate hover:text-graphite')}>
              {data?.user?.name ?? 'Account'}
            </NavLink>
          </nav>
        </div>
      </header>
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-5 md:px-6">
        <Outlet />
      </main>
    </div>
  );
}
