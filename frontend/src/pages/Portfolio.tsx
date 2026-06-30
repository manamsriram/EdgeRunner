import { useQuery } from '@tanstack/react-query'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import { getOrders, getPortfolioHistory, getPositions } from '../lib/api'

export default function Portfolio() {
  const { data: positions = [], isLoading: loadPos } = useQuery({
    queryKey: ['positions'],
    queryFn: () => getPositions().then((r) => r.data),
    refetchInterval: 60_000,
  })

  const { data: orders = [], isLoading: loadOrd } = useQuery({
    queryKey: ['orders'],
    queryFn: () => getOrders().then((r) => r.data),
    refetchInterval: 60_000,
  })

  const { data: history } = useQuery({
    queryKey: ['portfolio-history'],
    queryFn: () => getPortfolioHistory().then((r) => r.data),
    refetchInterval: 60_000,
  })

  const equityData =
    history?.timestamp.map((t, i) => ({
      date: t.slice(0, 10),
      equity: history.equity[i],
    })) ?? []

  return (
    <div className="flex flex-col gap-6">
      {/* Live Positions */}
      <section>
        <h2 className="text-xl font-bold text-white mb-3 tracking-tight">Live Positions</h2>
        {loadPos ? (
          <p className="text-zinc-500">Loading...</p>
        ) : positions.length === 0 ? (
          <div className="bg-zinc-900 rounded-xl p-6 text-zinc-500 border border-zinc-800">
            No open positions.
          </div>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-zinc-800">
            <table className="w-full text-sm text-zinc-300">
              <thead className="bg-zinc-800 text-zinc-500 uppercase text-xs">
                <tr>
                  {['Symbol', 'Qty', 'Avg Entry', 'Market Value', 'Unrealized P&L'].map((h) => (
                    <th key={h} className="px-4 py-3 text-left font-medium tracking-wider">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => (
                  <tr key={p.symbol} className="border-t border-zinc-800 hover:bg-zinc-900/60 transition-colors">
                    <td className="px-4 py-3 font-bold text-white">{p.symbol}</td>
                    <td className="px-4 py-3 font-mono">{p.qty.toFixed(4)}</td>
                    <td className="px-4 py-3 font-mono">${p.avg_entry_price.toFixed(2)}</td>
                    <td className="px-4 py-3 font-mono">${p.market_value.toFixed(2)}</td>
                    <td
                      className={`px-4 py-3 font-mono font-semibold ${
                        p.unrealized_pl >= 0 ? 'text-emerald-400' : 'text-red-400'
                      }`}
                    >
                      {p.unrealized_pl >= 0 ? '+' : ''}${p.unrealized_pl.toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Equity Curve */}
      <section>
        <h2 className="text-xl font-bold text-white mb-3 tracking-tight">Equity Curve</h2>
        {equityData.length === 0 ? (
          <div className="bg-zinc-900 rounded-xl p-6 text-zinc-500 border border-zinc-800">
            No portfolio history yet - run the scheduler to populate.
          </div>
        ) : (
          <div className="bg-zinc-900 rounded-xl p-4 border border-zinc-800">
            <ResponsiveContainer width="100%" height={280}>
              <LineChart data={equityData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
                <XAxis dataKey="date" tick={{ fill: '#71717a', fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis tick={{ fill: '#71717a', fontSize: 11 }} tickFormatter={(v) => `$${v.toLocaleString()}`} width={80} />
                <Tooltip
                  contentStyle={{ background: '#18181b', border: '1px solid #3f3f46', borderRadius: 8 }}
                  labelStyle={{ color: '#71717a' }}
                  itemStyle={{ color: '#34d399' }}
                  formatter={(v) => [`$${Number(v).toLocaleString()}`, 'Equity']}
                />
                <Line type="monotone" dataKey="equity" stroke="#34d399" dot={false} strokeWidth={2} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </section>

      {/* Recent Orders */}
      <section>
        <h2 className="text-xl font-bold text-white mb-3 tracking-tight">Recent Orders</h2>
        {loadOrd ? (
          <p className="text-zinc-500">Loading...</p>
        ) : orders.length === 0 ? (
          <div className="bg-zinc-900 rounded-xl p-6 text-zinc-500 border border-zinc-800">
            No orders recorded yet.
          </div>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-zinc-800">
            <table className="w-full text-sm text-zinc-300">
              <thead className="bg-zinc-800 text-zinc-500 uppercase text-xs">
                <tr>
                  {['Time', 'Symbol', 'Side', 'Notional', 'Status'].map((h) => (
                    <th key={h} className="px-4 py-3 text-left font-medium tracking-wider">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {orders.map((o) => (
                  <tr key={o.id} className="border-t border-zinc-800 hover:bg-zinc-900/60 transition-colors">
                    <td className="px-4 py-3 text-zinc-500 font-mono text-xs">{new Date(o.ts).toLocaleString()}</td>
                    <td className="px-4 py-3 font-bold text-white">{o.symbol}</td>
                    <td className="px-4 py-3">
                      <span
                        className={`px-2 py-0.5 rounded text-xs font-semibold uppercase ${
                          o.side === 'buy' ? 'bg-emerald-950 text-emerald-400' : 'bg-red-950 text-red-400'
                        }`}
                      >
                        {o.side}
                      </span>
                    </td>
                    <td className="px-4 py-3 font-mono">${o.notional.toFixed(2)}</td>
                    <td className="px-4 py-3 text-zinc-500">{o.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}
