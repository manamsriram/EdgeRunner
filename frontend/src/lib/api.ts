import axios from 'axios'

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL ?? '/',
  withCredentials: true, // send the session cookie on cross-origin requests to the backend
  headers: { 'X-Requested-With': 'edgerunner' }, // CSRF guard: forms can't set this, only our own JS can
})

// ---- proposals ----
export const getProposals = () => api.get<Proposal[]>('/api/proposals')
export const approveProposal = (id: number) => api.post(`/api/proposals/${id}/approve`)
export const rejectProposal = (id: number) => api.post(`/api/proposals/${id}/reject`)

// ---- portfolio ----
export const getPositions = () => api.get<Position[]>('/api/portfolio/positions')
export const getOrders = () => api.get<Order[]>('/api/portfolio/orders')
export const getPortfolioHistory = () =>
  api.get<{ timestamp: string[]; equity: number[] }>('/api/portfolio/history')

// ---- performance ----
export const getPerformance = () => api.get<PerformanceMetrics>('/api/performance')

// ---- calendar ----
export const getCalendar = () => api.get<CalendarDay[]>('/api/calendar')

// ---- controls ----
export const getKillSwitch = () =>
  api.get<{ engaged: boolean; note: string | null }>('/api/controls/kill-switch')
export const engageKillSwitch = () => api.post('/api/controls/kill-switch/engage')
export const disengageKillSwitch = () => api.post('/api/controls/kill-switch/disengage')
export const getAutonomy = () => api.get<{ mode: string }>('/api/controls/autonomy')
export const setAutonomy = (mode: 'manual' | 'auto') =>
  api.post<{ mode: string }>('/api/controls/autonomy', { mode })
export const getRuns = () => api.get<RunEntry[]>('/api/controls/runs')

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
