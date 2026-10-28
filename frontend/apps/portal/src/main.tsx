import '@sentrix/ui/styles/base.css';
import { ApiError } from '@sentrix/api';
import { EmptyState, Spinner, ToastProvider, Wordmark } from '@sentrix/ui';
import { QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode, lazy, Suspense, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { createBrowserRouter, Link, Navigate, Outlet, RouterProvider, useParams } from 'react-router';
import { OwnerShell, PublicShell, RequireRole, StaffShell, TestBanner } from './layouts';
import { SESSION_KEY, homeFor, useSession } from './lib/api';
import { StepUpProvider } from './lib/stepup';
import { Activate } from './pages/auth/Activate';
import { Login } from './pages/auth/Login';
import { TwoFactor } from './pages/auth/TwoFactor';
import { Activity } from './pages/owner/Activity';
import { AlertPage } from './pages/owner/AlertPage';
import { Claim } from './pages/owner/Claim';
import { Home } from './pages/owner/Home';
import { Live } from './pages/owner/Live';
import { People } from './pages/owner/People';
import { AccountSettings, AlertSettings, DeviceSettings, HomeSettings, PrivacySettings, SettingsIndex } from './pages/owner/Settings';
import { ActionLink } from './pages/public/ActionLink';
import { Consent, ContactAlert, PrivacyNotice, VerifyEmail } from './pages/public/Contact';
import { Setup } from './pages/setup/Setup';

const Desk = lazy(() => import('./pages/staff/Desk').then((m) => ({ default: m.DeskQueue })));
const DeskRequest = lazy(() => import('./pages/staff/Desk').then((m) => ({ default: m.DeskRequest })));
const AdminOverview = lazy(() => import('./pages/staff/Admin').then((m) => ({ default: m.AdminOverview })));
const AdminCustomers = lazy(() => import('./pages/staff/Admin').then((m) => ({ default: m.AdminCustomers })));
const AdminCustomer = lazy(() => import('./pages/staff/Admin').then((m) => ({ default: m.AdminCustomer })));
const AdminDevices = lazy(() => import('./pages/staff/Admin').then((m) => ({ default: m.AdminDevices })));
const AdminStaff = lazy(() => import('./pages/staff/Admin').then((m) => ({ default: m.AdminStaff })));
const AdminAudit = lazy(() => import('./pages/staff/Admin').then((m) => ({ default: m.AdminAudit })));

const queryClient: QueryClient = new QueryClient({
  defaultOptions: { queries: { retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 2, refetchOnWindowFocus: true } },
  queryCache: new QueryCache({
    // A session that ended elsewhere: ask who we are again so the guards
    // send the person to sign in.
    onError: (e, q) => {
      if (e instanceof ApiError && e.code === 'not_signed_in' && q.queryKey[0] !== SESSION_KEY[0]) queryClient.invalidateQueries({ queryKey: SESSION_KEY });
    },
  }),
});

function Lazy({ children }: { children: ReactNode }) {
  return <Suspense fallback={<Spinner />}>{children}</Suspense>;
}

/** `/`: wherever this person belongs. */
function Landing() {
  const { data, isLoading } = useSession();
  if (isLoading) return <Spinner />;
  return <Navigate to={data?.signedIn ? homeFor(data) : '/login'} replace />;
}

/** The setup wizard has its own quiet frame: no tabs until it is done. */
function SetupShell() {
  const { data } = useSession();
  return (
    <div className="min-h-dvh bg-chalk">
      <TestBanner session={data} />
      <header className="mx-auto flex max-w-xl items-center justify-between px-4 py-3">
        <Wordmark className="text-graphite" />
        {data?.household?.setupDone ? (
          <Link to="/home" className="text-[13px] font-semibold text-slate">
            Close
          </Link>
        ) : null}
      </header>
      <Outlet />
    </div>
  );
}

/** Staff and household members share `/account`. */
function AccountPage() {
  const { data } = useSession();
  if (data?.role === 'owner' || data?.role === 'member') return <Navigate to="/settings/account" replace />;
  return <AccountSettings />;
}

function NotFound() {
  return (
    <div className="mx-auto max-w-md px-4 py-16">
      <EmptyState title="This page doesn’t exist" action={<Link to="/" className="font-semibold text-indigo underline">Go to SENTRIX</Link>}>
        The link may be old or mistyped.
      </EmptyState>
    </div>
  );
}

function To({ to }: { to: (p: Record<string, string | undefined>) => string }) {
  return <Navigate to={to(useParams())} replace />;
}

const HOUSEHOLD = ['owner', 'member'] as const;
const STAFF_DESK = ['operator'] as const;
const STAFF_ADMIN = ['company_admin'] as const;

const router = createBrowserRouter([
  { path: '/', element: <Landing /> },
  {
    element: <PublicShell />,
    children: [
      { path: '/login', element: <Login /> },
      { path: '/activate', element: <Activate /> },
      { path: '/activate/:code', element: <Activate /> },
      { path: '/consent/:token', element: <Consent /> },
      { path: '/c/:token', element: <ContactAlert /> },
      { path: '/a/:token', element: <ActionLink /> },
      { path: '/account/verify-email/:token', element: <VerifyEmail /> },
      { path: '/account/2fa', element: <RequireRole roles={['owner', 'member', 'operator', 'company_admin']}><TwoFactor /></RequireRole> },
    ],
  },
  { element: <PublicShell wide />, children: [{ path: '/privacy/notice', element: <PrivacyNotice /> }] },
  {
    element: (
      <RequireRole roles={['owner']}>
        <SetupShell />
      </RequireRole>
    ),
    children: [
      { path: '/setup', element: <Setup /> },
      { path: '/setup/:step', element: <Setup /> },
    ],
  },
  { path: '/live/:deviceId', element: <RequireRole roles={[...HOUSEHOLD]}><Live /></RequireRole> },
  {
    element: (
      <RequireRole roles={[...HOUSEHOLD]}>
        <StepUpProvider>
          <OwnerShell />
        </StepUpProvider>
      </RequireRole>
    ),
    children: [
      { path: '/home', element: <Home /> },
      { path: '/activity', element: <Activity /> },
      { path: '/people', element: <People /> },
      { path: '/alert/:id', element: <AlertPage /> },
      { path: '/sos/:id', element: <AlertPage /> },
      { path: '/claim', element: <Claim /> },
      { path: '/settings', element: <SettingsIndex /> },
      { path: '/settings/alerts', element: <AlertSettings /> },
      { path: '/settings/home', element: <HomeSettings /> },
      { path: '/settings/devices', element: <DeviceSettings /> },
      { path: '/settings/account', element: <AccountSettings /> },
      { path: '/settings/privacy', element: <PrivacySettings /> },
    ],
  },
  {
    element: (
      <RequireRole roles={[...STAFF_DESK]}>
        <StaffShell area="desk" />
      </RequireRole>
    ),
    children: [
      { path: '/desk', element: <Lazy><Desk /></Lazy> },
      { path: '/desk/:id', element: <Lazy><DeskRequest /></Lazy> },
      { path: '/desk/requests/:id', element: <To to={(p) => `/desk/${p.id}`} /> },
    ],
  },
  {
    element: (
      <RequireRole roles={[...STAFF_ADMIN]}>
        <StaffShell area="admin" />
      </RequireRole>
    ),
    children: [
      { path: '/admin', element: <Lazy><AdminOverview /></Lazy> },
      { path: '/admin/customers', element: <Lazy><AdminCustomers /></Lazy> },
      { path: '/admin/customers/:id', element: <Lazy><AdminCustomer /></Lazy> },
      { path: '/admin/customers/:id/letter', element: <To to={(p) => `/admin/customers/${p.id}`} /> },
      { path: '/admin/devices', element: <Lazy><AdminDevices /></Lazy> },
      { path: '/admin/devices/:id/label', element: <Navigate to="/admin/devices" replace /> },
      { path: '/admin/operators', element: <Lazy><AdminStaff /></Lazy> },
      { path: '/admin/audit', element: <Lazy><AdminAudit /></Lazy> },
    ],
  },
  {
    element: (
      <RequireRole roles={['operator', 'company_admin', 'owner', 'member']}>
        <StepUpProvider>
          <StaffAccountShell />
        </StepUpProvider>
      </RequireRole>
    ),
    children: [{ path: '/account', element: <AccountPage /> }],
  },
  // Paths of the old server-rendered Portal, so links in old messages work.
  { path: '/onboarding', element: <Navigate to="/setup" replace /> },
  { path: '/onboarding/:step', element: <Navigate to="/setup" replace /> },
  { path: '/household', element: <Navigate to="/settings/home" replace /> },
  { path: '/household/contacts', element: <Navigate to="/people" replace /> },
  { path: '/household/members', element: <Navigate to="/people" replace /> },
  { path: '/household/audit', element: <Navigate to="/activity" replace /> },
  { path: '/sos', element: <Navigate to="/activity" replace /> },
  { path: '/privacy', element: <Navigate to="/settings/privacy" replace /> },
  { path: '/privacy/erase', element: <Navigate to="/settings/privacy" replace /> },
  { path: '/privacy/export', element: <Navigate to="/settings/privacy" replace /> },
  { path: '/account/password', element: <Navigate to="/account" replace /> },
  { path: '/account/sessions', element: <Navigate to="/account" replace /> },
  { path: '/customers', element: <Navigate to="/admin/customers" replace /> },
  { path: '/devices', element: <Navigate to="/admin/devices" replace /> },
  { path: '/operators', element: <Navigate to="/admin/operators" replace /> },
  { path: '/audit', element: <Navigate to="/admin/audit" replace /> },
  { path: '*', element: <NotFound /> },
]);

/** `/account` for staff: their own shell; household people are sent on. */
function StaffAccountShell() {
  const { data } = useSession();
  if (data?.role === 'operator') return <StaffShell area="desk" />;
  if (data?.role === 'company_admin') return <StaffShell area="admin" />;
  return <Outlet />;
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
);
