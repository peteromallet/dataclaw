import { useState } from 'react';
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
  onStatusChange?: (newStatus: string) => void;
}

function scoreStyle(score: number): { background: string; color: string } {
  if (score >= 4) return { background: '#dcfce7', color: '#166534' };
  if (score === 3) return { background: '#fef3c7', color: '#92400e' };
  return { background: '#fee2e2', color: '#991b1b' };
}

export function TraceCard({ session, selected, onSelect, onStatusChange }: TraceCardProps) {
  const navigate = useNavigate();
  const totalTokens = session.input_tokens + session.output_tokens;
  const totalMsgs = session.user_messages + session.assistant_messages;
  const [showComment, setShowComment] = useState(false);
  const [commentText, setCommentText] = useState(session.reviewer_notes ?? '');

  const quickAction = async (status: ReviewStatus) => {
    await api.sessions.update(session.session_id, { status });
    onStatusChange?.(status);
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
          {session.ai_quality_score != null && (
            <span style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              width: 22,
              height: 22,
              borderRadius: '50%',
              fontSize: 11,
              fontWeight: 700,
              flexShrink: 0,
              ...scoreStyle(session.ai_quality_score),
            }}>
              {session.ai_quality_score}
            </span>
          )}
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

        {/* Inline comment form */}
        {showComment && (
          <div
            style={{ marginTop: 8, padding: 8, background: '#f9fafb', borderRadius: 6, border: '1px solid #e5e7eb' }}
            onClick={(e) => e.stopPropagation()}
          >
            <textarea
              value={commentText}
              onChange={(e) => setCommentText(e.target.value)}
              placeholder="Add a note..."
              rows={2}
              style={{
                width: '100%',
                padding: '6px 8px',
                border: '1px solid #d1d5db',
                borderRadius: 4,
                fontSize: 12,
                fontFamily: 'inherit',
                boxSizing: 'border-box',
                resize: 'vertical',
              }}
            />
            <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
              <button
                onClick={async () => {
                  await api.sessions.update(session.session_id, { notes: commentText });
                  setShowComment(false);
                  onStatusChange?.('note saved');
                }}
                style={{ padding: '4px 10px', fontSize: 11, fontWeight: 600, background: '#2563eb', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer' }}
              >
                Save
              </button>
              <button
                onClick={() => { setShowComment(false); setCommentText(session.reviewer_notes ?? ''); }}
                style={{ padding: '4px 10px', fontSize: 11, fontWeight: 600, background: '#fff', color: '#374151', border: '1px solid #d1d5db', borderRadius: 4, cursor: 'pointer' }}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Quick actions */}
      <div
        style={{ display: 'flex', gap: 4, flexShrink: 0 }}
        onClick={(e) => e.stopPropagation()}
      >
        <button
          onClick={() => setShowComment(!showComment)}
          style={{ ...actionPillStyle, background: '#f3f4f6', color: '#374151', borderColor: '#d1d5db' }}
        >
          Note
        </button>
        {session.review_status !== 'shortlisted' && (
          <button onClick={() => quickAction('shortlisted')} style={{ ...actionPillStyle, background: '#eff6ff', color: '#1d4ed8', borderColor: '#bfdbfe' }}>
            Shortlist
          </button>
        )}
        {session.review_status !== 'approved' && (
          <button onClick={() => quickAction('approved')} style={{ ...actionPillStyle, background: '#f0fdf4', color: '#166534', borderColor: '#bbf7d0' }}>
            Approve
          </button>
        )}
        {session.review_status !== 'blocked' && (
          <button onClick={() => quickAction('blocked')} style={{ ...actionPillStyle, background: '#fef2f2', color: '#991b1b', borderColor: '#fecaca' }}>
            Block
          </button>
        )}
      </div>
    </div>
  );
}

const actionPillStyle: React.CSSProperties = {
  padding: '4px 10px',
  border: '1px solid #e5e7eb',
  borderRadius: 12,
  background: '#fff',
  cursor: 'pointer',
  fontSize: 11,
  fontWeight: 600,
  whiteSpace: 'nowrap',
};
