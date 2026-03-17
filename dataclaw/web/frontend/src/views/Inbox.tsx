import { useState, useEffect, useCallback } from 'react';
import type { Session, ReviewStatus } from '../types.ts';
import { api } from '../api.ts';
import { TraceCard } from '../components/TraceCard.tsx';
import { FilterBar } from '../components/FilterBar.tsx';

interface Stats {
  total: number;
  by_status: Record<string, number>;
  by_source: Record<string, number>;
  by_project: Record<string, number>;
}

interface Filters {
  status: string | null;
  source: string | null;
  project: string | null;
  sort: string;
  order: string;
}

const PAGE_SIZE = 50;

const STATUS_TABS: { key: string | null; label: string }[] = [
  { key: null, label: 'Total' },
  { key: 'new', label: 'New' },
  { key: 'shortlisted', label: 'Shortlisted' },
  { key: 'approved', label: 'Approved' },
  { key: 'blocked', label: 'Blocked' },
];

export function Inbox() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [filters, setFilters] = useState<Filters>({
    status: null,
    source: null,
    project: null,
    sort: 'start_time',
    order: 'desc',
  });
  const [stats, setStats] = useState<Stats>({ total: 0, by_status: {}, by_source: {}, by_project: {} });
  const [loading, setLoading] = useState(false);
  const [offset, setOffset] = useState(0);
  const [scanning, setScanning] = useState(false);

  const loadStats = useCallback(async () => {
    try {
      const s = await api.stats();
      setStats(s);
    } catch {
      // ignore
    }
  }, []);

  const loadSessions = useCallback(async (currentOffset: number, append: boolean) => {
    setLoading(true);
    try {
      const data = await api.sessions.list({
        status: filters.status,
        source: filters.source,
        project: filters.project,
        sort: filters.sort,
        order: filters.order,
        limit: PAGE_SIZE,
        offset: currentOffset,
      });
      setSessions((prev) => (append ? [...prev, ...data] : data));
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, [filters]);

  // Load on mount and when filters change
  useEffect(() => {
    setOffset(0);
    setSelected(new Set());
    loadSessions(0, false);
    loadStats();
  }, [filters, loadSessions, loadStats]);

  const handleLoadMore = () => {
    const newOffset = offset + PAGE_SIZE;
    setOffset(newOffset);
    loadSessions(newOffset, true);
  };

  const handleRefresh = async () => {
    setScanning(true);
    try {
      await api.scan();
    } catch {
      // ignore
    } finally {
      setScanning(false);
    }
    setOffset(0);
    setSelected(new Set());
    await Promise.all([loadSessions(0, false), loadStats()]);
  };

  const handleSelect = (id: string, checked: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const handleBulkAction = async (status: ReviewStatus) => {
    setLoading(true);
    try {
      await Promise.all(
        Array.from(selected).map((id) => api.sessions.update(id, { status })),
      );
      setSelected(new Set());
      await Promise.all([loadSessions(0, false), loadStats()]);
      setOffset(0);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  };

  const handleStatusTabClick = (statusKey: string | null) => {
    setFilters((prev) => ({ ...prev, status: statusKey }));
  };

  const getStatusCount = (key: string | null): number => {
    if (key === null) return stats.total;
    return stats.by_status[key] ?? 0;
  };

  return (
    <div style={{ maxWidth: 960, margin: '0 auto', padding: '24px 16px' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20 }}>
        <h1 style={{ fontSize: 22, fontWeight: 700, margin: 0, color: '#111827' }}>Inbox</h1>
        <button
          onClick={handleRefresh}
          disabled={scanning}
          style={{
            padding: '8px 16px',
            borderRadius: 6,
            border: '1px solid #d1d5db',
            background: '#fff',
            fontSize: 13,
            fontWeight: 500,
            cursor: scanning ? 'not-allowed' : 'pointer',
            color: '#374151',
          }}
        >
          {scanning ? 'Scanning...' : 'Refresh'}
        </button>
      </div>

      {/* Stats bar */}
      <div style={{ display: 'flex', gap: 0, marginBottom: 16, borderRadius: 8, overflow: 'hidden', border: '1px solid #e5e7eb' }}>
        {STATUS_TABS.map((tab) => {
          const isActive = filters.status === tab.key;
          return (
            <button
              key={tab.label}
              onClick={() => handleStatusTabClick(tab.key)}
              style={{
                flex: 1,
                padding: '12px 8px',
                border: 'none',
                borderRight: '1px solid #e5e7eb',
                background: isActive ? '#f0f9ff' : '#fafafa',
                cursor: 'pointer',
                textAlign: 'center',
              }}
            >
              <div style={{ fontSize: 20, fontWeight: 700, color: isActive ? '#1d4ed8' : '#111827' }}>
                {getStatusCount(tab.key)}
              </div>
              <div style={{ fontSize: 12, color: isActive ? '#1d4ed8' : '#6b7280', marginTop: 2 }}>
                {tab.label}
              </div>
            </button>
          );
        })}
      </div>

      {/* Filter bar */}
      <div style={{ marginBottom: 16 }}>
        <FilterBar filters={filters} onChange={setFilters} />
      </div>

      {/* Bulk actions bar */}
      {selected.size > 0 && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            padding: '10px 16px',
            marginBottom: 12,
            background: '#f0f9ff',
            borderRadius: 8,
            border: '1px solid #bfdbfe',
          }}
        >
          <span style={{ fontSize: 13, fontWeight: 600, color: '#1e40af', marginRight: 8 }}>
            {selected.size} selected
          </span>
          <button onClick={() => handleBulkAction('shortlisted')} style={bulkBtnStyle}>
            Shortlist All
          </button>
          <button onClick={() => handleBulkAction('approved')} style={{ ...bulkBtnStyle, color: '#166534', borderColor: '#bbf7d0' }}>
            Approve All
          </button>
          <button onClick={() => handleBulkAction('blocked')} style={{ ...bulkBtnStyle, color: '#991b1b', borderColor: '#fecaca' }}>
            Block All
          </button>
          <button
            onClick={() => setSelected(new Set())}
            style={{ ...bulkBtnStyle, color: '#6b7280', borderColor: '#d1d5db' }}
          >
            Clear Selection
          </button>
        </div>
      )}

      {/* Sessions list */}
      <div style={{ border: '1px solid #e5e7eb', borderRadius: 8, overflow: 'hidden', background: '#fff' }}>
        {sessions.length === 0 && !loading && (
          <div style={{ padding: 40, textAlign: 'center', color: '#9ca3af', fontSize: 14 }}>
            No sessions found. Try adjusting your filters or click Refresh to scan for new sessions.
          </div>
        )}
        {sessions.map((s) => (
          <TraceCard
            key={s.session_id}
            session={s}
            selected={selected.has(s.session_id)}
            onSelect={handleSelect}
            onStatusChange={() => {
              loadSessions(0, false);
              loadStats();
              setOffset(0);
            }}
          />
        ))}
      </div>

      {/* Load more */}
      {sessions.length > 0 && sessions.length % PAGE_SIZE === 0 && (
        <div style={{ textAlign: 'center', marginTop: 16 }}>
          <button
            onClick={handleLoadMore}
            disabled={loading}
            style={{
              padding: '10px 24px',
              borderRadius: 6,
              border: '1px solid #d1d5db',
              background: '#fff',
              fontSize: 13,
              fontWeight: 500,
              cursor: loading ? 'not-allowed' : 'pointer',
              color: '#374151',
            }}
          >
            {loading ? 'Loading...' : 'Load more'}
          </button>
        </div>
      )}

      {/* Loading indicator */}
      {loading && sessions.length === 0 && (
        <div style={{ textAlign: 'center', padding: 40, color: '#9ca3af', fontSize: 14 }}>
          Loading...
        </div>
      )}
    </div>
  );
}

const bulkBtnStyle: React.CSSProperties = {
  padding: '6px 12px',
  borderRadius: 6,
  border: '1px solid #bfdbfe',
  background: '#fff',
  fontSize: 12,
  fontWeight: 600,
  cursor: 'pointer',
  color: '#1e40af',
};
