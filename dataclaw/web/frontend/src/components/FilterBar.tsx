import { useEffect, useState } from 'react';
import type { ProjectSummary } from '../types.ts';
import { api } from '../api.ts';

interface Filters {
  status: string | null;
  source: string | null;
  project: string | null;
  sort: string;
  order: string;
}

interface FilterBarProps {
  filters: Filters;
  onChange: (f: Filters) => void;
}

export function FilterBar({ filters, onChange }: FilterBarProps) {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);

  useEffect(() => {
    api.projects().then(setProjects).catch(() => {});
  }, []);

  const set = (key: keyof Filters, val: string | null) => {
    onChange({ ...filters, [key]: val || null });
  };

  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
      <select
        value={filters.status ?? ''}
        onChange={(e) => set('status', e.target.value)}
        style={selectStyle}
      >
        <option value="">All statuses</option>
        <option value="new">New</option>
        <option value="shortlisted">Shortlisted</option>
        <option value="approved">Approved</option>
        <option value="blocked">Blocked</option>
      </select>

      <select
        value={filters.source ?? ''}
        onChange={(e) => set('source', e.target.value)}
        style={selectStyle}
      >
        <option value="">All sources</option>
        <option value="claude">Claude Code</option>
        <option value="codex">Codex</option>
        <option value="openclaw">OpenClaw</option>
      </select>

      <select
        value={filters.project ?? ''}
        onChange={(e) => set('project', e.target.value)}
        style={selectStyle}
      >
        <option value="">All projects</option>
        {projects.map((p) => (
          <option key={`${p.source}:${p.project}`} value={p.project}>
            {p.project} ({p.session_count})
          </option>
        ))}
      </select>

      <select
        value={`${filters.sort}:${filters.order}`}
        onChange={(e) => {
          const [sort, order] = e.target.value.split(':');
          onChange({ ...filters, sort, order });
        }}
        style={selectStyle}
      >
        <option value="start_time:desc">Newest first</option>
        <option value="start_time:asc">Oldest first</option>
        <option value="sensitivity_score:desc">Highest risk</option>
        <option value="input_tokens:desc">Most tokens</option>
        <option value="tool_uses:desc">Most tool use</option>
      </select>
    </div>
  );
}

const selectStyle: React.CSSProperties = {
  padding: '6px 10px',
  borderRadius: 6,
  border: '1px solid #d1d5db',
  fontSize: 13,
  background: '#fff',
  cursor: 'pointer',
};
