import '@sentrix/ui/styles/base.css';
import { Spinner, ToastProvider } from '@sentrix/ui';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode, lazy, Suspense, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { createBrowserRouter, Navigate, RouterProvider, useLocation } from 'react-router';
import { useSession } from './lib/api';
import { Login } from './pages/Login';
import { WallView } from './pages/WallView';

const AdminApp = lazy(() => import('./pages/admin/AdminApp'));
const WifiSetup = lazy(() => import('./pages/WifiSetup'));

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

function RequireSignIn({ children, admin }: { children: ReactNode; admin?: boolean }) {
  const { data, isLoading, isError } = useSession();
  const location = useLocation();
  if (isLoading) return <Spinner />;
  if (isError || !data?.signedIn) return <Navigate to={`/login?next=${encodeURIComponent(location.pathname)}`} replace />;
  if (admin && !data.canAdmin) return <Navigate to="/" replace />;
  return <>{children}</>;
}

const router = createBrowserRouter([
  { path: '/login', element: <Login /> },
  {
    path: '/setup/wifi',
    element: (
      <Suspense fallback={<Spinner />}>
        <WifiSetup />
      </Suspense>
    ),
  },
  {
    path: '/admin/*',
    element: (
      <RequireSignIn admin>
        <Suspense fallback={<Spinner />}>
          <AdminApp />
        </Suspense>
      </RequireSignIn>
    ),
  },
  { path: '/dashboard', element: <Navigate to="/" replace /> },
  { path: '/live', element: <Navigate to="/" replace /> },
  { path: '/events', element: <Navigate to="/admin/activity" replace /> },
  { path: '/alerts', element: <Navigate to="/admin/activity" replace /> },
  { path: '/evidence', element: <Navigate to="/admin/activity" replace /> },
  { path: '/dispatch', element: <Navigate to="/admin/activity" replace /> },
  { path: '/sos', element: <Navigate to="/admin/sos" replace /> },
  { path: '/node', element: <Navigate to="/admin/hardware" replace /> },
  { path: '/authorized', element: <Navigate to="/admin/faces" replace /> },
  {
    path: '/',
    element: (
      <RequireSignIn>
        <WallView />
      </RequireSignIn>
    ),
  },
  { path: '*', element: <Navigate to="/" replace /> },
]);

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
);
