import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { Toaster } from 'react-hot-toast'
import Login from './pages/Login'
import Analysis from './pages/Analysis'
import Approvals from './pages/Approvals'
import Portfolio from './pages/Portfolio'
import Controls from './pages/Controls'
import Performance from './pages/Performance'
import ProtectedLayout from './components/ProtectedLayout'

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 55_000, retry: 1 } },
})

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route element={<ProtectedLayout />}>
            <Route path="/portfolio" element={<Portfolio />} />
            <Route path="/performance" element={<Performance />} />
            <Route path="/approvals" element={<Approvals />} />
            <Route path="/analysis" element={<Analysis />} />
            <Route path="/controls" element={<Controls />} />
            <Route path="/" element={<Navigate to="/portfolio" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
      <Toaster
        position="top-right"
        toastOptions={{ style: { background: '#1e293b', color: '#e2e8f0', border: '1px solid #334155' } }}
      />
    </QueryClientProvider>
  )
}
