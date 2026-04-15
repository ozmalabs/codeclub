import { useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', icon: '📊' },
  { to: '/tasks', label: 'Tasks', icon: '📋' },
  { to: '/models', label: 'Models', icon: '🤖' },
  { to: '/maps', label: 'Maps', icon: '🗺️' },
  { to: '/tournament', label: 'Tournament', icon: '⚔️' },
  { to: '/git', label: 'Git', icon: '🌿' },
  { to: '/hardware', label: 'Hardware', icon: '🖥️' },
  { to: '/settings', label: 'Settings', icon: '⚙️' },
];

export default function Layout() {
  const [sidebarOpen, setSidebarOpen] = useState(false);

  return (
    <div className="flex min-h-screen bg-slate-900 text-slate-300">
      {sidebarOpen && (
        <button
          type="button"
          aria-label="Close navigation"
          onClick={() => setSidebarOpen(false)}
          className="fixed inset-0 z-30 bg-slate-950/70 md:hidden"
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-56 flex-col border-r border-slate-700 bg-slate-800 transition-transform duration-200 md:static md:translate-x-0 ${
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="flex items-start justify-between border-b border-slate-700 px-4 py-4">
          <div>
            <h1 className="text-lg font-bold text-sky-400">🏏 codeclub</h1>
            <p className="mt-0.5 text-xs text-slate-500">caveman make code good</p>
          </div>
          <button
            type="button"
            onClick={() => setSidebarOpen(false)}
            className="rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:border-slate-500 hover:text-slate-200 md:hidden"
          >
            ✕
          </button>
        </div>
        <nav className="flex-1 py-2">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              onClick={() => setSidebarOpen(false)}
              className={({ isActive }) =>
                `flex items-center gap-3 px-4 py-2 text-sm transition-colors ${
                  isActive
                    ? 'border-r-2 border-sky-400 bg-sky-900/40 text-sky-300'
                    : 'text-slate-400 hover:bg-slate-800/50 hover:text-slate-200'
                }`
              }
            >
              <span>{item.icon}</span>
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-slate-700 px-4 py-3 text-xs text-slate-600">
          Brought to you by{' '}
          <a href="https://ozmalabs.com" className="text-sky-600 hover:text-sky-400">
            Ozma
          </a>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex items-center justify-between border-b border-slate-700 bg-slate-900/95 px-4 py-3 backdrop-blur md:hidden">
          <div>
            <h1 className="text-base font-bold text-sky-400">🏏 codeclub</h1>
            <p className="text-[11px] text-slate-500">caveman keep tools handy</p>
          </div>
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            className="rounded-md border border-slate-700 px-3 py-2 text-sm text-slate-200 hover:border-slate-500"
          >
            ☰
          </button>
        </header>

        <main className="flex-1 overflow-auto bg-slate-900">
          <div className="mx-auto max-w-7xl p-4 sm:p-6">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
