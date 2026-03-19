import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom';
import { Inbox } from './views/Inbox.tsx';
import { Search } from './views/Search.tsx';
import SessionDetail from './views/SessionDetail.tsx';
import { Bundles } from './views/Bundles.tsx';
import { Policies } from './views/Policies.tsx';
import { Dashboard } from './views/Dashboard.tsx';

const NAV_ITEMS = [
  { to: '/dashboard', label: 'Dashboard' },
  { to: '/', label: 'Sessions' },
  { to: '/search', label: 'Search' },
  { to: '/bundles', label: 'Exports' },
  { to: '/policies', label: 'Rules' },
];

function Sidebar() {
  return (
    <nav style={{
      width: 180,
      background: '#f9fafb',
      borderRight: '1px solid #e5e7eb',
      display: 'flex',
      flexDirection: 'column',
      padding: '16px 0',
      flexShrink: 0,
    }}>
      <div style={{
        padding: '0 16px 20px',
        fontSize: 15,
        fontWeight: 700,
        color: '#111827',
        letterSpacing: '-0.01em',
      }}>
        DataClaw
      </div>
      {NAV_ITEMS.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === '/'}
          style={({ isActive }) => ({
            display: 'block',
            padding: '8px 16px',
            fontSize: 13,
            fontWeight: isActive ? 600 : 400,
            color: isActive ? '#1d4ed8' : '#6b7280',
            background: isActive ? '#eff6ff' : 'transparent',
            textDecoration: 'none',
            borderLeft: isActive ? '3px solid #1d4ed8' : '3px solid transparent',
          })}
        >
          {item.label}
        </NavLink>
      ))}
      <div style={{ flex: 1 }} />
      <div style={{ padding: '8px 16px', fontSize: 11, color: '#9ca3af' }}>
        Workbench v0.1
      </div>
    </nav>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <div style={{
        display: 'flex',
        height: '100vh',
        fontFamily: 'system-ui, -apple-system, sans-serif',
        color: '#111827',
      }}>
        <Sidebar />
        <main style={{ flex: 1, overflow: 'auto', background: '#fff' }}>
          <Routes>
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/" element={<Inbox />} />
            <Route path="/search" element={<Search />} />
            <Route path="/session/:id" element={<SessionDetail />} />
            <Route path="/bundles" element={<Bundles />} />
            <Route path="/policies" element={<Policies />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}
