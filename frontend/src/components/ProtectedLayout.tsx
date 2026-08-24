import { NavLink, Outlet } from 'react-router-dom'

// `live: true` groups the tabs backed by the real-money GCP deployment. They're
// separated in the nav rather than mixed in, so clicking Approvals is never an
// accident about which account you're acting on.
const NAV = [
  { to: '/portfolio', label: 'Portfolio' },
  { to: '/performance', label: 'Performance' },
  { to: '/calendar', label: 'Calendar' },
  { to: '/analysis', label: 'Analysis' },
]

const LIVE_NAV = [
  { to: '/calendar/live', label: 'Calendar' },
  { to: '/approvals', label: 'Approvals' },
  { to: '/controls', label: 'Controls' },
  { to: '/logs', label: 'Logs' },
]

export default function ProtectedLayout() {
  return (
    <div className="flex min-h-screen bg-zinc-950">
      {/* Sidebar */}
      <aside className="w-52 bg-zinc-900 flex flex-col py-6 px-4 gap-1 border-r border-zinc-800 shrink-0">
        <div className="text-white font-bold text-lg mb-5 px-2 tracking-tight">
          EdgeRunner
        </div>
        <div className="text-zinc-600 text-[11px] font-semibold uppercase tracking-wider px-3 pb-1">
          Paper
        </div>
        {NAV.map(({ to, label }) => (
          <NavLink
            key={to}
            to={to}
            end
            className={({ isActive }) =>
              `px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                isActive
                  ? 'bg-emerald-600 text-white'
                  : 'text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100'
              }`
            }
          >
            {label}
          </NavLink>
        ))}

        <div className="text-amber-500/70 text-[11px] font-semibold uppercase tracking-wider px-3 pt-5 pb-1">
          Real money
        </div>
        {LIVE_NAV.map(({ to, label }) => (
          <NavLink
            key={to}
            to={to}
            end
            className={({ isActive }) =>
              `px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                isActive
                  ? 'bg-amber-600 text-white'
                  : 'text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100'
              }`
            }
          >
            {label}
          </NavLink>
        ))}
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto p-6">
        <Outlet />
      </main>
    </div>
  )
}
