import { NavLink } from 'react-router-dom';
import { LayoutDashboard, Rss, HardDrive, Bot } from 'lucide-react';

const links = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/channels', icon: Rss, label: 'Channels' },
  { to: '/downloaders', icon: HardDrive, label: 'Downloaders' },
  { to: '/agents', icon: Bot, label: 'Agents' },
];

export default function Sidebar() {
  return (
    <aside className="w-60 bg-slate-900 text-white flex flex-col shrink-0">
      <div className="p-5 border-b border-slate-700">
        <h1 className="text-lg font-bold tracking-tight">RSS Downloader</h1>
        <p className="text-xs text-slate-400 mt-0.5">Auto-download service</p>
      </div>
      <nav className="flex-1 p-3 space-y-1">
        {links.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                isActive
                  ? 'bg-blue-600 text-white'
                  : 'text-slate-300 hover:bg-slate-800 hover:text-white'
              }`
            }
          >
            <Icon size={18} />
            {label}
          </NavLink>
        ))}
      </nav>
      <div className="p-4 border-t border-slate-700 text-xs text-slate-500">
        v0.1.0
      </div>
    </aside>
  );
}
