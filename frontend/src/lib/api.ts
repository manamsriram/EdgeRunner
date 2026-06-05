import axios from 'axios'

export const api = axios.create({
  baseURL: '/',
  withCredentials: true,
})

let _refreshing: Promise<void> | null = null

api.interceptors.response.use(
  (r) => r,
  async (err) => {
    const original = err.config
    // Never intercept the refresh endpoint itself — prevents circular deadlock
    if (
      err.response?.status === 401 &&
      !original._retry &&
      !original.url?.includes('/auth/refresh')
    ) {
      original._retry = true
      if (!_refreshing) {
        _refreshing = api
          .post('/auth/refresh')
          .then(() => {})
          .finally(() => { _refreshing = null })
      }
      try {
        await _refreshing
        return api.request(original)
      } catch {
        _refreshing = null
        return Promise.reject(err)
      }
    }
    return Promise.reject(err)
  }
)

// ---- auth ----
export const login = (username: string, password: string) =>
  api.post<{ username: string; full_name: string }>('/auth/login', { username, password })

export const register = (data: {
  username: string
  email: string
  full_name: string
  password: string
  confirm_password: string
}) => api.post('/auth/register', data)

export const logout = () => api.post('/auth/logout')

export const getMe = () => api.get<{ username: string; email: string; full_name: string }>('/auth/me')

export const getHistory = () =>
  api.get<Array<{ query: string; response: string; timestamp: string }>>('/auth/history')

// ---- proposals ----
export const getProposals = () => api.get<Proposal[]>('/api/proposals')
export const approveProposal = (id: number) => api.post(`/api/proposals/${id}/approve`)
export const rejectProposal = (id: number) => api.post(`/api/proposals/${id}/reject`)

// ---- portfolio ----
export const getPositions = () => api.get<Position[]>('/api/portfolio/positions')
export const getOrders = () => api.get<Order[]>('/api/portfolio/orders')
export const getPortfolioHistory = () =>
  api.get<{ timestamp: string[]; equity: number[] }>('/api/portfolio/history')

// ---- controls ----
export const getKillSwitch = () => api.get<{ engaged: boolean }>('/api/controls/kill-switch')
export const engageKillSwitch = () => api.post('/api/controls/kill-switch/engage')
export const disengageKillSwitch = () => api.post('/api/controls/kill-switch/disengage')
export const getAutonomy = () => api.get<{ mode: string }>('/api/controls/autonomy')
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

export interface RunEntry {
  id: number
  started_at: string
  strategy: string
  mode: string
  note: string | null
}
