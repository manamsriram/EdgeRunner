import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { Toaster } from 'react-hot-toast'
import Analysis from './pages/Analysis'
import Approvals from './pages/Approvals'
import Calendar from './pages/Calendar'
import Portfolio from './pages/Portfolio'
import Controls from './pages/Controls'
import Logs from './pages/Logs'
import Performance from './pages/Performance'
import ProtectedLayout from './components/ProtectedLayout'
import RequireAuth from './components/RequireAuth'

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 55_000, retry: 1 } },
})

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<ProtectedLayout />}>
            <Route path="/portfolio" element={<Portfolio />} />
            <Route path="/performance" element={<Performance />} />
            <Route path="/calendar" element={<Calendar />} />
            {/* Live calendar is auth-gated in the UI and again server-side via
                PROTECT_READS on the GCP deployment — the route guard alone only
                hides the tab, it doesn't protect the endpoint. */}
            <Route path="/calendar/live" element={<RequireAuth><Calendar account="live" /></RequireAuth>} />
            <Route path="/approvals" element={<RequireAuth><Approvals /></RequireAuth>} />
            <Route path="/analysis" element={<Analysis />} />
            <Route path="/controls" element={<RequireAuth><Controls /></RequireAuth>} />
            <Route path="/logs" element={<RequireAuth><Logs /></RequireAuth>} />
            <Route path="/" element={<Navigate to="/calendar" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
      <Toaster
        position="top-right"
        toastOptions={{ style: { background: '#18181b', color: '#e4e4e7', border: '1px solid #3f3f46' } }}
      />
    </QueryClientProvider>
  )
}
