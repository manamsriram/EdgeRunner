import axios from 'axios'
import type { InternalAxiosRequestConfig } from 'axios'
import { supabase } from './supabase'

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL ?? '/',
})

// Second backend: the real-money deployment on the GCP VM. Separate service and
// separate database from paper — the schema has no account column, so the two
// must never share one. Approvals and Controls talk only to this one; paper runs
// unattended on AUTONOMY=auto and has nothing to approve.
export const liveApi = axios.create({
  baseURL: import.meta.env.VITE_LIVE_API_URL ?? import.meta.env.VITE_API_URL ?? '/',
})

// Bearer token from the current Supabase session, not a cookie: frontend (Vercel)
// and backend (Render) are different domains, and browsers increasingly block or
// partition third-party cookies regardless of SameSite/Secure. Public routes just
// never check this header server-side, so sending it (or not) on those is harmless.
const attachBearer = async (config: InternalAxiosRequestConfig) => {
  const { data } = await supabase.auth.getSession()
  const token = data.session?.access_token
  if (token) {
    config.headers = config.headers ?? {}
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
}
api.interceptors.request.use(attachBearer)
liveApi.interceptors.request.use(attachBearer)

// ---- proposals (live money only) ----
export const getProposals = () => liveApi.get<Proposal[]>('/api/proposals')
export const approveProposal = (id: number) => liveApi.post(`/api/proposals/${id}/approve`)
export const rejectProposal = (id: number) => liveApi.post(`/api/proposals/${id}/reject`)

// ---- portfolio ----
export const getPositions = () => api.get<Position[]>('/api/portfolio/positions')
export const getOrders = () => api.get<Order[]>('/api/portfolio/orders')
export const getPortfolioHistory = () =>
  api.get<{ timestamp: string[]; equity: number[] }>('/api/portfolio/history')

// ---- performance ----
export const getPerformance = () => api.get<PerformanceMetrics>('/api/performance')

// ---- calendar ----
export const getCalendar = () => api.get<CalendarDay[]>('/api/calendar')
export const getLiveCalendar = () => liveApi.get<CalendarDay[]>('/api/calendar')

// ---- controls (live money only) ----
export const getKillSwitch = () =>
  liveApi.get<{ engaged: boolean; note: string | null }>('/api/controls/kill-switch')
export const engageKillSwitch = () => liveApi.post('/api/controls/kill-switch/engage')
export const disengageKillSwitch = () => liveApi.post('/api/controls/kill-switch/disengage')
export const getAutonomy = () => liveApi.get<{ mode: string }>('/api/controls/autonomy')
export const setAutonomy = (mode: 'manual' | 'auto') =>
  liveApi.post<{ mode: string }>('/api/controls/autonomy', { mode })
export const getRuns = () => liveApi.get<RunEntry[]>('/api/controls/runs')

export type JournalUnit = 'edgerunner' | 'caddy' | 'edgerunner-deploy'
export const getLogs = (unit: JournalUnit, lines: number, since?: string) =>
  liveApi.get<{ unit: string; lines: string[] }>('/api/controls/logs', {
    params: { unit, lines, ...(since ? { since } : {}) },
  })

// ---- types ----
export interface Proposal {
  id: number
  symbol: string
  side: string
  notional: number
  ref_price: number
  reason: string
  created_at: string
  status: string
}

export interface Position {
  symbol: string
  qty: number
  avg_entry_price: number
  market_value: number
  unrealized_pl: number
}

export interface Order {
  id: number
  client_order_id: string
  ts: string
  symbol: string
  side: string
  notional: number
  status: string
  broker_order_id: string | null
}

export interface PerformanceMetrics {
  days_active: number
  trade_count: number
  sharpe: number
  max_drawdown: number
  win_rate: number
  profit_factor: number | null   // null when infinity (all trades profitable); 0.0 when no closed trades
  total_return: number
  benchmark_spy_return: number | null
  benchmark_btc_return: number | null
  verdict: 'PASS' | 'FAIL' | 'INSUFFICIENT_DATA'
  failing_checks: string[]
  strategy_signals: Record<string, number>
}

export interface CalendarTrade {
  symbol: string
  strategy: string | null
  pnl: number
  pnl_pct: number
  qty: number
  open_price: number
  close_price: number
  open_date: string
  close_date: string
}

export interface CalendarDay {
  date: string
  pnl_pct: number | null
  pnl_amount: number | null
  trades: CalendarTrade[]
}

export interface RunEntry {
  id: number
  started_at: string
  strategy: string
  mode: string
  note: string | null
}
