import { useNavigate } from 'react-router-dom';
import type { Session, ReviewStatus } from '../types.ts';
import { BadgeChip } from './BadgeChip.tsx';
import { api } from '../api.ts';

const SOURCE_ICONS: Record<string, string> = {
  claude: 'CC',
  codex: 'CX',
  openclaw: 'OC',
};

function formatDuration(seconds: number | null): string {
  if (!seconds) return '-';
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

function formatTime(iso: string | null): string {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffDays = Math.floor(diffMs / 86400000);
    if (diffDays === 0) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (diffDays === 1) return 'Yesterday';
    if (diffDays < 7) return `${diffDays}d ago`;
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  } catch {
    return '';
  }
}

interface TraceCardProps {
  session: Session;
  selected: boolean;
  onSelect: (id: string, checked: boolean) => void;
  onStatusChange?: () => void;
}

export function TraceCard({ session, selected, onSelect, onStatusChange }: TraceCardProps) {
  const navigate = useNavigate();
  const totalTokens = session.input_tokens + session.output_tokens;
  const totalMsgs = session.user_messages + session.assistant_messages;

  const quickAction = async (status: ReviewStatus) => {
    await api.sessions.update(session.session_id, { status });
    onStatusChange?.();
  };

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 12,
        padding: '12px 16px',
        borderBottom: '1px solid #e5e7eb',
        background: selected ? '#f0f9ff' : '#fff',
        cursor: 'pointer',
      }}
      onClick={() => navigate(`/session/${encodeURIComponent(session.session_id)}`)}
    >
      {/* Checkbox */}
      <input
        type="checkbox"
        checked={selected}
        onClick={(e) => e.stopPropagation()}
        onChange={(e) => onSelect(session.session_id, e.target.checked)}
        style={{ marginTop: 4, cursor: 'pointer' }}
      />

      {/* Source icon */}
      <div
        style={{
          width: 32,
          height: 32,
          borderRadius: 6,
          background: '#f3f4f6',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 11,
          fontWeight: 700,
          color: '#6b7280',
          flexShrink: 0,
        }}
      >
        {SOURCE_ICONS[session.source] ?? session.source.slice(0, 2).toUpperCase()}
      </div>

      {/* Main content */}
      <div style={{ flex: 1, minWidth: 0 }}>
        {/* Title row */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontWeight: 600, fontSize: 14, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {session.display_title}
          </span>
          <span style={{ fontSize: 11, color: '#9ca3af', flexShrink: 0 }}>
            {formatTime(session.start_time)}
          </span>
        </div>

        {/* Meta row */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 4, fontSize: 12, color: '#6b7280' }}>
          <span>{session.project}</span>
          <span style={{ color: '#d1d5db' }}>|</span>
          <span>{session.model?.split('-').slice(0, 2).join('-') ?? 'unknown'}</span>
          <span style={{ color: '#d1d5db' }}>|</span>
          <span>{totalMsgs} msgs</span>
          <span style={{ color: '#d1d5db' }}>|</span>
          <span>{formatTokens(totalTokens)} tokens</span>
          <span style={{ color: '#d1d5db' }}>|</span>
          <span>{session.tool_uses} tools</span>
          {session.duration_seconds && (
            <>
              <span style={{ color: '#d1d5db' }}>|</span>
              <span>{formatDuration(session.duration_seconds)}</span>
            </>
          )}
        </div>

        {/* Badges row */}
        <div style={{ display: 'flex', gap: 4, marginTop: 6, flexWrap: 'wrap' }}>
          <BadgeChip kind="status" value={session.review_status} />
          {session.outcome_badge && session.outcome_badge !== 'unknown' && (
            <BadgeChip kind="outcome" value={session.outcome_badge} />
          )}
          {session.value_badges?.map((b) => (
            <BadgeChip key={b} kind="value" value={b} />
          ))}
          {session.risk_badges?.map((b) => (
            <BadgeChip key={b} kind="risk" value={b} />
          ))}
        </div>
      </div>

      {/* Quick actions */}
      <div
        style={{ display: 'flex', gap: 4, flexShrink: 0 }}
        onClick={(e) => e.stopPropagation()}
      >
        {session.review_status !== 'shortlisted' && (
          <button onClick={() => quickAction('shortlisted')} style={actionBtnStyle} title="Shortlist">
            +
          </button>
        )}
        {session.review_status !== 'approved' && (
          <button onClick={() => quickAction('approved')} style={{ ...actionBtnStyle, color: '#166534' }} title="Approve">
            &#10003;
          </button>
        )}
        {session.review_status !== 'blocked' && (
          <button onClick={() => quickAction('blocked')} style={{ ...actionBtnStyle, color: '#991b1b' }} title="Block">
            &#10005;
          </button>
        )}
      </div>
    </div>
  );
}

const actionBtnStyle: React.CSSProperties = {
  width: 28,
  height: 28,
  border: '1px solid #e5e7eb',
  borderRadius: 6,
  background: '#fff',
  cursor: 'pointer',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontSize: 14,
  fontWeight: 700,
};
