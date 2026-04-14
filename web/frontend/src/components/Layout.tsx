import { NavLink, Outlet } from 'react-router-dom';

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', icon: '📊' },
  { to: '/tasks', label: 'Tasks', icon: '📋' },
  { to: '/models', label: 'Models', icon: '🤖' },
  { to: '/maps', label: 'Maps', icon: '🗺️' },
  { to: '/tournament', label: 'Tournament', icon: '⚔️' },
  { to: '/hardware', label: 'Hardware', icon: '🖥️' },
  { to: '/settings', label: 'Settings', icon: '⚙️' },
];

export default function Layout() {
  return (
    <div className="flex h-screen">
      {/* Sidebar */}
      <aside className="w-56 bg-[#1e293b] border-r border-slate-700 flex flex-col">
        <div className="px-4 py-4 border-b border-slate-700">
          <h1 className="text-lg font-bold text-sky-400">🏏 codeclub</h1>
          <p className="text-xs text-slate-500 mt-0.5">caveman make code good</p>
        </div>
        <nav className="flex-1 py-2">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) =>
                `flex items-center gap-3 px-4 py-2 text-sm transition-colors ${
                  isActive
                    ? 'bg-sky-900/40 text-sky-300 border-r-2 border-sky-400'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50'
                }`
              }
            >
              <span>{item.icon}</span>
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="px-4 py-3 border-t border-slate-700 text-xs text-slate-600">
          Brought to you by{' '}
          <a href="https://ozmalabs.com" className="text-sky-600 hover:text-sky-400">
            Ozma
          </a>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto bg-[#0f172a]">
        <div className="max-w-7xl mx-auto p-6">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
