import { useState, useCallback } from 'react';
import type { Session } from '../types.ts';
import { api } from '../api.ts';
import { TraceCard } from '../components/TraceCard.tsx';

export function Search() {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<Session[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [debounceTimer, setDebounceTimer] = useState<ReturnType<typeof setTimeout> | null>(null);

  const doSearch = useCallback(async (q: string) => {
    if (!q.trim()) {
      setResults([]);
      setSearched(false);
      return;
    }
    setLoading(true);
    setSearched(true);
    try {
      const data = await api.search(q.trim());
      setResults(data);
    } catch {
      setResults([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleChange = (value: string) => {
    setQuery(value);
    if (debounceTimer) clearTimeout(debounceTimer);
    const timer = setTimeout(() => doSearch(value), 300);
    setDebounceTimer(timer);
  };

  // no-op for selection in search view
  const handleSelect = () => {};

  return (
    <div style={{ maxWidth: 960, margin: '0 auto', padding: '24px 16px' }}>
      {/* Header */}
      <h1 style={{ fontSize: 22, fontWeight: 700, margin: '0 0 20px 0', color: '#111827' }}>Search</h1>

      {/* Search input */}
      <div style={{ marginBottom: 20 }}>
        <input
          type="text"
          value={query}
          onChange={(e) => handleChange(e.target.value)}
          placeholder="Search sessions by content, project, model..."
          style={{
            width: '100%',
            padding: '14px 16px',
            borderRadius: 8,
            border: '1px solid #d1d5db',
            fontSize: 15,
            outline: 'none',
            boxSizing: 'border-box',
            background: '#fff',
          }}
        />
      </div>

      {/* Loading indicator */}
      {loading && (
        <div style={{ textAlign: 'center', padding: 40, color: '#9ca3af', fontSize: 14 }}>
          Searching...
        </div>
      )}

      {/* Results */}
      {!loading && results.length > 0 && (
        <div style={{ border: '1px solid #e5e7eb', borderRadius: 8, overflow: 'hidden', background: '#fff' }}>
          <div style={{ padding: '10px 16px', borderBottom: '1px solid #e5e7eb', fontSize: 13, color: '#6b7280' }}>
            {results.length} result{results.length !== 1 ? 's' : ''}
          </div>
          {results.map((s) => (
            <TraceCard
              key={s.session_id}
              session={s}
              selected={false}
              onSelect={handleSelect}
            />
          ))}
        </div>
      )}

      {/* No results */}
      {!loading && searched && results.length === 0 && (
        <div style={{ textAlign: 'center', padding: 40, color: '#9ca3af', fontSize: 14 }}>
          No results found for "{query}"
        </div>
      )}

      {/* Empty state before search */}
      {!loading && !searched && (
        <div style={{ textAlign: 'center', padding: 60, color: '#d1d5db', fontSize: 14 }}>
          Type to search across all session content
        </div>
      )}
    </div>
  );
}
