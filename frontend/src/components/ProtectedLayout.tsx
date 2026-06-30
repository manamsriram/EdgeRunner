import { NavLink, Outlet } from 'react-router-dom'

const NAV = [
  { to: '/portfolio', label: 'Portfolio' },
  { to: '/performance', label: 'Performance' },
  { to: '/calendar', label: 'Calendar' },
  { to: '/approvals', label: 'Approvals' },
  { to: '/analysis', label: 'Analysis' },
  { to: '/controls', label: 'Controls' },
]

export default function ProtectedLayout() {
  return (
    <div className="flex min-h-screen bg-zinc-950">
      {/* Sidebar */}
      <aside className="w-52 bg-zinc-900 flex flex-col py-6 px-4 gap-1 border-r border-zinc-800 shrink-0">
        <div className="text-white font-bold text-lg mb-5 px-2 tracking-tight">
          EdgeRunner
        </div>
        {NAV.map(({ to, label }) => (
          <NavLink
            key={to}
            to={to}
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
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto p-6">
        <Outlet />
      </main>
    </div>
  )
}
