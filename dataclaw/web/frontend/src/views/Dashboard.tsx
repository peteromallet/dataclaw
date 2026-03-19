import { useState, useEffect, useCallback } from 'react';
import type { DashboardData } from '../types.ts';
import { api } from '../api.ts';
import { LABELS } from '../components/BadgeChip.tsx';

function formatNumber(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
  return String(n);
}

function BarRow({ label, value, max, color = '#3b82f6' }: {
  label: string;
  value: number;
  max: number;
  color?: string;
}) {
  const pct = max > 0 ? (value / max) * 100 : 0;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 0' }}>
      <div style={{ width: 140, fontSize: 13, color: '#374151', flexShrink: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={label}>
        {label}
      </div>
      <div style={{ flex: 1, background: '#f3f4f6', borderRadius: 4, height: 20 }}>
        <div style={{ width: `${pct}%`, background: color, borderRadius: 4, height: 20, minWidth: pct > 0 ? 2 : 0 }} />
      </div>
      <div style={{ width: 50, fontSize: 12, color: '#6b7280', textAlign: 'right', flexShrink: 0 }}>
        {formatNumber(value)}
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ border: '1px solid #e5e7eb', borderRadius: 8, padding: 16, marginBottom: 16 }}>
      <h3 style={{ fontSize: 14, fontWeight: 600, color: '#111827', margin: '0 0 12px 0' }}>{title}</h3>
      {children}
    </div>
  );
}

export function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api.dashboard();
      setData(d);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading && !data) {
    return (
      <div style={{ maxWidth: 960, margin: '0 auto', padding: '24px 16px' }}>
        <div style={{ textAlign: 'center', padding: 40, color: '#9ca3af', fontSize: 14 }}>Loading...</div>
      </div>
    );
  }

  if (!data) return null;

  const { summary, activity, by_outcome_badge, by_value_badge, by_risk_badge, by_task_type, by_model, tokens_by_source } = data;

  const activityMax = Math.max(...activity.map(a => a.count), 0);
  const sourceMax = Math.max(...tokens_by_source.map(s => s.input_tokens + s.output_tokens), 0);
  const modelMax = Math.max(...by_model.map(m => m.count), 0);
  const taskMax = Math.max(...by_task_type.map(t => t.count), 0);
  const outcomeMax = Math.max(...by_outcome_badge.map(b => b.count), 0);
  const valueMax = Math.max(...by_value_badge.map(b => b.count), 0);
  const riskMax = Math.max(...by_risk_badge.map(b => b.count), 0);
  const tokenSourceMax = Math.max(...tokens_by_source.map(s => Math.max(s.input_tokens, s.output_tokens)), 0);

  const summaryCards = [
    { label: 'Total Sessions', value: summary.total_sessions },
    { label: 'Total Tokens', value: summary.total_tokens },
    { label: 'Projects', value: summary.unique_projects },
    { label: 'Sources', value: summary.unique_sources },
  ];

  return (
    <div style={{ maxWidth: 960, margin: '0 auto', padding: '24px 16px' }}>
      <h1 style={{ fontSize: 22, fontWeight: 700, margin: '0 0 4px 0', color: '#111827' }}>Dashboard</h1>
      <p style={{ fontSize: 13, color: '#6b7280', margin: '0 0 16px 0' }}>Overview of your collection</p>

      {/* Summary cards */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 20 }}>
        {summaryCards.map(c => (
          <div key={c.label} style={{
            flex: 1,
            padding: '16px 12px',
            border: '1px solid #e5e7eb',
            borderRadius: 8,
            textAlign: 'center',
            background: '#fafafa',
          }}>
            <div style={{ fontSize: 24, fontWeight: 700, color: '#111827' }}>{formatNumber(c.value)}</div>
            <div style={{ fontSize: 12, color: '#6b7280', marginTop: 4 }}>{c.label}</div>
          </div>
        ))}
      </div>

      {/* Activity */}
      {activity.length > 0 && (
        <Section title="Activity (last 30 days)">
          {activity.map(a => (
            <BarRow key={a.day} label={a.day} value={a.count} max={activityMax} color="#3b82f6" />
          ))}
        </Section>
      )}

      {/* Source distribution */}
      {tokens_by_source.length > 0 && (
        <Section title="Source Distribution">
          {tokens_by_source.map(s => (
            <BarRow key={s.source} label={s.source} value={s.input_tokens + s.output_tokens} max={sourceMax} color="#8b5cf6" />
          ))}
        </Section>
      )}

      {/* Model distribution */}
      {by_model.length > 0 && (
        <Section title="Model Distribution">
          {by_model.map(m => (
            <BarRow key={m.model} label={m.model} value={m.count} max={modelMax} color="#10b981" />
          ))}
        </Section>
      )}

      {/* Task type distribution */}
      {by_task_type.length > 0 && (
        <Section title="Task Type Distribution">
          {by_task_type.map(t => (
            <BarRow key={t.task_type} label={LABELS[t.task_type] ?? t.task_type.replace(/_/g, ' ')} value={t.count} max={taskMax} color="#f59e0b" />
          ))}
        </Section>
      )}

      {/* Badge distributions */}
      {by_outcome_badge.length > 0 && (
        <Section title="Outcome Badges">
          {by_outcome_badge.map(b => (
            <BarRow key={b.outcome_badge} label={LABELS[b.outcome_badge] ?? b.outcome_badge.replace(/_/g, ' ')} value={b.count} max={outcomeMax} color="#6366f1" />
          ))}
        </Section>
      )}

      {by_value_badge.length > 0 && (
        <Section title="Value Badges">
          {by_value_badge.map(b => (
            <BarRow key={b.badge} label={LABELS[b.badge] ?? b.badge.replace(/_/g, ' ')} value={b.count} max={valueMax} color="#14b8a6" />
          ))}
        </Section>
      )}

      {by_risk_badge.length > 0 && (
        <Section title="Risk Badges">
          {by_risk_badge.map(b => (
            <BarRow key={b.badge} label={LABELS[b.badge] ?? b.badge.replace(/_/g, ' ')} value={b.count} max={riskMax} color="#ef4444" />
          ))}
        </Section>
      )}

      {/* Token usage by source */}
      {tokens_by_source.length > 0 && (
        <Section title="Token Usage by Source">
          {tokens_by_source.map(s => (
            <div key={s.source} style={{ marginBottom: 8 }}>
              <div style={{ fontSize: 13, color: '#374151', marginBottom: 4 }}>{s.source}</div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <div style={{ width: 50, fontSize: 11, color: '#6b7280', textAlign: 'right', flexShrink: 0 }}>Input</div>
                <div style={{ flex: 1, background: '#f3f4f6', borderRadius: 4, height: 16 }}>
                  <div style={{ width: `${tokenSourceMax > 0 ? (s.input_tokens / tokenSourceMax) * 100 : 0}%`, background: '#3b82f6', borderRadius: 4, height: 16, minWidth: s.input_tokens > 0 ? 2 : 0 }} />
                </div>
                <div style={{ width: 50, fontSize: 11, color: '#6b7280', textAlign: 'right', flexShrink: 0 }}>{formatNumber(s.input_tokens)}</div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 2 }}>
                <div style={{ width: 50, fontSize: 11, color: '#6b7280', textAlign: 'right', flexShrink: 0 }}>Output</div>
                <div style={{ flex: 1, background: '#f3f4f6', borderRadius: 4, height: 16 }}>
                  <div style={{ width: `${tokenSourceMax > 0 ? (s.output_tokens / tokenSourceMax) * 100 : 0}%`, background: '#93c5fd', borderRadius: 4, height: 16, minWidth: s.output_tokens > 0 ? 2 : 0 }} />
                </div>
                <div style={{ width: 50, fontSize: 11, color: '#6b7280', textAlign: 'right', flexShrink: 0 }}>{formatNumber(s.output_tokens)}</div>
              </div>
            </div>
          ))}
        </Section>
      )}
    </div>
  );
}
