import { NavLink, Outlet } from 'react-router-dom'

const NAV = [
  { to: '/portfolio', label: 'Portfolio' },
  { to: '/performance', label: 'Performance' },
  { to: '/approvals', label: 'Approvals' },
  { to: '/analysis', label: 'Analysis' },
  { to: '/controls', label: 'Controls' },
]

export default function ProtectedLayout() {
  return (
    <div className="flex min-h-screen bg-slate-900">
      {/* Sidebar */}
      <aside className="w-52 bg-slate-800 flex flex-col py-6 px-4 gap-2 border-r border-slate-700 shrink-0">
        <div className="text-white font-bold text-lg mb-4 px-2">Trading Bot</div>
        {NAV.map(({ to, label }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              `px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                isActive
                  ? 'bg-blue-600 text-white'
                  : 'text-slate-300 hover:bg-slate-700 hover:text-white'
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
