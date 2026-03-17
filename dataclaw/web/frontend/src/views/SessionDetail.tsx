import { useEffect, useState, useRef, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { api } from '../api.ts';
import type { SessionDetail as SessionDetailType, Message, ToolUse, ReviewStatus } from '../types.ts';
import { BadgeChip } from '../components/BadgeChip.tsx';

/* ------------------------------------------------------------------ */
/*  Helpers                                                           */
/* ------------------------------------------------------------------ */

function formatDuration(seconds: number | null): string {
  if (seconds == null) return '--';
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function formatTime(ts: string | null | undefined): string {
  if (!ts) return '--';
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return text.slice(0, max) + '...';
}

function sensitivityColor(score: number): string {
  if (score >= 0.7) return '#ef4444';
  if (score >= 0.4) return '#f59e0b';
  return '#22c55e';
}

/** Render text with [REDACTED] spans highlighted. */
function RedactedText({ text }: { text: string }) {
  const parts = text.split(/(\[REDACTED\])/g);
  return (
    <>
      {parts.map((part, i) =>
        part === '[REDACTED]' ? (
          <span
            key={i}
            style={{
              background: '#fee2e2',
              color: '#991b1b',
              borderRadius: 3,
              padding: '0 3px',
              fontWeight: 600,
            }}
          >
            [REDACTED]
          </span>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  Sub-components                                                    */
/* ------------------------------------------------------------------ */

function ThinkingBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ margin: '6px 0' }}>
      <button
        onClick={() => setOpen(!open)}
        style={{
          background: 'none',
          border: 'none',
          color: '#6b7280',
          cursor: 'pointer',
          fontSize: 12,
          padding: 0,
          textDecoration: 'underline',
        }}
      >
        {open ? 'Hide thinking' : 'Show thinking'}
      </button>
      {open && (
        <pre
          style={{
            background: '#fefce8',
            border: '1px solid #fde68a',
            borderRadius: 6,
            padding: 10,
            fontSize: 12,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            marginTop: 4,
            maxHeight: 300,
            overflow: 'auto',
          }}
        >
          {text}
        </pre>
      )}
    </div>
  );
}

function ToolUseCard({ tu }: { tu: ToolUse }) {
  const [open, setOpen] = useState(false);
  const statusColor =
    tu.status === 'success' || tu.status === 'ok'
      ? '#166534'
      : tu.status === 'error'
        ? '#991b1b'
        : '#6b7280';
  const statusBg =
    tu.status === 'success' || tu.status === 'ok'
      ? '#dcfce7'
      : tu.status === 'error'
        ? '#fee2e2'
        : '#f3f4f6';

  const renderBlock = (data: Record<string, unknown> | string) => {
    const text = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
    return (
      <pre
        style={{
          background: '#f9fafb',
          border: '1px solid #e5e7eb',
          borderRadius: 4,
          padding: 8,
          fontSize: 11,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          maxHeight: 250,
          overflow: 'auto',
          margin: '4px 0',
        }}
      >
        <RedactedText text={text} />
      </pre>
    );
  };

  return (
    <div
      style={{
        border: '1px solid #e5e7eb',
        borderRadius: 6,
        margin: '6px 0',
        overflow: 'hidden',
      }}
    >
      <div
        onClick={() => setOpen(!open)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '6px 10px',
          cursor: 'pointer',
          background: '#f9fafb',
          fontSize: 13,
        }}
      >
        <span style={{ fontFamily: 'monospace', fontWeight: 600 }}>{tu.tool}</span>
        <span
          style={{
            fontSize: 10,
            padding: '1px 6px',
            borderRadius: 9999,
            background: statusBg,
            color: statusColor,
            fontWeight: 500,
          }}
        >
          {tu.status}
        </span>
        <span style={{ marginLeft: 'auto', color: '#9ca3af', fontSize: 11 }}>
          {open ? '\u25B2' : '\u25BC'}
        </span>
      </div>
      {open && (
        <div style={{ padding: '8px 10px' }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: '#6b7280', marginBottom: 2 }}>
            Input
          </div>
          {renderBlock(tu.input)}
          <div
            style={{ fontSize: 11, fontWeight: 600, color: '#6b7280', marginBottom: 2, marginTop: 8 }}
          >
            Output
          </div>
          {renderBlock(tu.output)}
        </div>
      )}
    </div>
  );
}

function MessageCard({
  msg,
  index,
  refCallback,
}: {
  msg: Message;
  index: number;
  refCallback: (el: HTMLDivElement | null) => void;
}) {
  const isUser = msg.role === 'user';
  return (
    <div
      ref={refCallback}
      data-msg-index={index}
      style={{
        borderLeft: `3px solid ${isUser ? '#93c5fd' : '#d1d5db'}`,
        padding: '10px 14px',
        marginBottom: 12,
        background: '#fff',
        borderRadius: '0 6px 6px 0',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginBottom: 6,
          fontSize: 12,
          color: '#6b7280',
        }}
      >
        <span style={{ fontWeight: 600, color: isUser ? '#2563eb' : '#374151' }}>
          {isUser ? 'User' : 'Assistant'}
        </span>
        <span>#{index}</span>
        {msg.timestamp && <span>{formatTime(msg.timestamp)}</span>}
      </div>

      {msg.thinking && <ThinkingBlock text={msg.thinking} />}

      <div
        style={{
          fontSize: 13,
          lineHeight: 1.6,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        <RedactedText text={msg.content} />
      </div>

      {msg.tool_uses && msg.tool_uses.length > 0 && (
        <div style={{ marginTop: 8 }}>
          {msg.tool_uses.map((tu, i) => (
            <ToolUseCard key={i} tu={tu} />
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Timeline entry builder                                            */
/* ------------------------------------------------------------------ */

interface TimelineEntry {
  index: number;
  label: string;
  kind: 'user' | 'assistant' | 'tool';
}

function buildTimeline(messages: Message[]): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  messages.forEach((msg, i) => {
    const preview = truncate(msg.content.replace(/\n/g, ' ').trim(), 30);
    entries.push({
      index: i,
      label: `${msg.role === 'user' ? 'User' : 'Asst'}: ${preview || '(empty)'}`,
      kind: msg.role,
    });
    if (msg.tool_uses) {
      msg.tool_uses.forEach((tu) => {
        entries.push({
          index: i,
          label: `Tool: ${tu.tool}`,
          kind: 'tool',
        });
      });
    }
  });
  return entries;
}

/* ------------------------------------------------------------------ */
/*  Review statuses                                                   */
/* ------------------------------------------------------------------ */

const REVIEW_STATUSES: ReviewStatus[] = ['new', 'shortlisted', 'approved', 'blocked'];

/* ------------------------------------------------------------------ */
/*  Main component                                                    */
/* ------------------------------------------------------------------ */

export default function SessionDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [session, setSession] = useState<SessionDetailType | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Review form state
  const [reviewStatus, setReviewStatus] = useState<string>('new');
  const [reviewNotes, setReviewNotes] = useState('');
  const [reviewReason, setReviewReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);

  // Refs for scroll targets
  const msgRefs = useRef<Map<number, HTMLDivElement>>(new Map());

  const setMsgRef = useCallback(
    (index: number) => (el: HTMLDivElement | null) => {
      if (el) {
        msgRefs.current.set(index, el);
      } else {
        msgRefs.current.delete(index);
      }
    },
    [],
  );

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    api.sessions
      .get(id)
      .then((data) => {
        setSession(data);
        setReviewStatus(data.review_status);
        setReviewNotes(data.reviewer_notes ?? '');
        setReviewReason(data.selection_reason ?? '');
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [id]);

  const scrollToMessage = (index: number) => {
    const el = msgRefs.current.get(index);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      // Flash highlight
      el.style.background = '#fef3c7';
      setTimeout(() => {
        el.style.background = '#fff';
      }, 800);
    }
  };

  const handleSave = async () => {
    if (!id) return;
    setSaving(true);
    setSaveMsg(null);
    try {
      await api.sessions.update(id, {
        status: reviewStatus,
        notes: reviewNotes || undefined,
        reason: reviewReason || undefined,
      });
      setSaveMsg('Saved');
      setTimeout(() => setSaveMsg(null), 2000);
    } catch (e: unknown) {
      setSaveMsg(`Error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  /* Loading / error states */
  if (loading) {
    return (
      <div style={{ padding: 40, textAlign: 'center', color: '#6b7280' }}>Loading session...</div>
    );
  }
  if (error) {
    return (
      <div style={{ padding: 40, textAlign: 'center' }}>
        <div style={{ color: '#991b1b', marginBottom: 12 }}>Error: {error}</div>
        <button onClick={() => navigate('/')} style={linkBtnStyle}>
          Back
        </button>
      </div>
    );
  }
  if (!session) {
    return (
      <div style={{ padding: 40, textAlign: 'center', color: '#6b7280' }}>Session not found.</div>
    );
  }

  const timeline = buildTimeline(session.messages);

  /* ---------------------------------------------------------------- */
  /*  Render                                                          */
  /* ---------------------------------------------------------------- */

  return (
    <div style={{ display: 'flex', height: '100vh', fontFamily: 'system-ui, sans-serif' }}>
      {/* ---- Left pane: Timeline ---- */}
      <div
        style={{
          width: 200,
          minWidth: 200,
          borderRight: '1px solid #e5e7eb',
          overflowY: 'auto',
          background: '#fafafa',
          padding: '10px 0',
          fontSize: 12,
        }}
      >
        <div style={{ padding: '0 10px 8px', borderBottom: '1px solid #e5e7eb' }}>
          <button onClick={() => navigate('/')} style={linkBtnStyle}>
            &larr; Back
          </button>
        </div>
        <div style={{ padding: '8px 10px 4px', fontWeight: 700, fontSize: 11, color: '#9ca3af' }}>
          TIMELINE ({session.messages.length})
        </div>
        {timeline.map((entry, i) => {
          const kindColor =
            entry.kind === 'user' ? '#2563eb' : entry.kind === 'tool' ? '#7c3aed' : '#374151';
          return (
            <div
              key={i}
              onClick={() => scrollToMessage(entry.index)}
              style={{
                padding: '4px 10px',
                cursor: 'pointer',
                borderLeft: `2px solid transparent`,
                lineHeight: 1.4,
              }}
              onMouseEnter={(e) => {
                (e.currentTarget as HTMLDivElement).style.background = '#e5e7eb';
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLDivElement).style.background = 'transparent';
              }}
            >
              <span style={{ color: kindColor, fontWeight: 500 }}>{entry.label}</span>
            </div>
          );
        })}
      </div>

      {/* ---- Center pane: Transcript ---- */}
      <div
        style={{
          flex: 1,
          overflowY: 'auto',
          padding: '16px 20px',
          background: '#f3f4f6',
        }}
      >
        <h2 style={{ margin: '0 0 4px', fontSize: 18 }}>{session.display_title}</h2>
        <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 16 }}>
          {session.project} &middot; {session.source}
          {session.model && <> &middot; {session.model}</>}
        </div>

        {session.messages.length === 0 && (
          <div style={{ color: '#9ca3af', fontStyle: 'italic' }}>No messages in this session.</div>
        )}

        {session.messages.map((msg, i) => (
          <MessageCard key={i} msg={msg} index={i} refCallback={setMsgRef(i)} />
        ))}
      </div>

      {/* ---- Right pane: Metadata ---- */}
      <div
        style={{
          width: 280,
          minWidth: 280,
          borderLeft: '1px solid #e5e7eb',
          overflowY: 'auto',
          padding: 14,
          fontSize: 12,
          background: '#fff',
        }}
      >
        {/* Metadata section */}
        <Section title="Session Info">
          <MetaRow label="ID" value={session.session_id} mono />
          <MetaRow label="Source" value={session.source} />
          <MetaRow label="Model" value={session.model ?? '--'} />
          <MetaRow label="Branch" value={session.git_branch ?? '--'} />
          <MetaRow label="Task type" value={session.task_type ?? '--'} />
          <MetaRow label="Started" value={formatTime(session.start_time)} />
          <MetaRow label="Duration" value={formatDuration(session.duration_seconds)} />
          <MetaRow
            label="Tokens"
            value={`${formatTokens(session.input_tokens)} in / ${formatTokens(session.output_tokens)} out`}
          />
          <MetaRow
            label="Messages"
            value={`${session.user_messages} user / ${session.assistant_messages} asst`}
          />
          <MetaRow label="Tool uses" value={String(session.tool_uses)} />
          {session.bundle_id && <MetaRow label="Bundle" value={session.bundle_id} mono />}
        </Section>

        {/* Badges */}
        <Section title="Badges">
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            <BadgeChip kind="status" value={session.review_status} />
            {session.outcome_badge && (
              <BadgeChip kind="outcome" value={session.outcome_badge} />
            )}
            {session.value_badges.map((b) => (
              <BadgeChip key={b} kind="value" value={b} />
            ))}
            {session.risk_badges.map((b) => (
              <BadgeChip key={b} kind="risk" value={b} />
            ))}
          </div>
        </Section>

        {/* Sensitivity */}
        <Section title="Sensitivity">
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <div
              style={{
                flex: 1,
                height: 6,
                background: '#e5e7eb',
                borderRadius: 3,
                overflow: 'hidden',
              }}
            >
              <div
                style={{
                  width: `${Math.round(session.sensitivity_score * 100)}%`,
                  height: '100%',
                  background: sensitivityColor(session.sensitivity_score),
                  borderRadius: 3,
                  transition: 'width 0.3s',
                }}
              />
            </div>
            <span style={{ fontWeight: 600, color: sensitivityColor(session.sensitivity_score) }}>
              {(session.sensitivity_score * 100).toFixed(0)}%
            </span>
          </div>
        </Section>

        {/* Files touched */}
        {session.files_touched.length > 0 && (
          <Section title={`Files Touched (${session.files_touched.length})`}>
            <ul style={{ margin: 0, padding: '0 0 0 14px', lineHeight: 1.8 }}>
              {session.files_touched.map((f, i) => (
                <li key={i} style={{ fontFamily: 'monospace', fontSize: 11, wordBreak: 'break-all' }}>
                  {f}
                </li>
              ))}
            </ul>
          </Section>
        )}

        {/* Commands run */}
        {session.commands_run.length > 0 && (
          <Section title={`Commands Run (${session.commands_run.length})`}>
            <ul style={{ margin: 0, padding: '0 0 0 14px', lineHeight: 1.8 }}>
              {session.commands_run.map((c, i) => (
                <li key={i} style={{ fontFamily: 'monospace', fontSize: 11, wordBreak: 'break-all' }}>
                  {c}
                </li>
              ))}
            </ul>
          </Section>
        )}

        {/* Review form */}
        <Section title="Review">
          <label style={labelStyle}>Status</label>
          <select
            value={reviewStatus}
            onChange={(e) => setReviewStatus(e.target.value)}
            style={inputStyle}
          >
            {REVIEW_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.charAt(0).toUpperCase() + s.slice(1)}
              </option>
            ))}
          </select>

          <label style={labelStyle}>Selection Reason</label>
          <input
            type="text"
            value={reviewReason}
            onChange={(e) => setReviewReason(e.target.value)}
            placeholder="Why this session was selected..."
            style={inputStyle}
          />

          <label style={labelStyle}>Reviewer Notes</label>
          <textarea
            value={reviewNotes}
            onChange={(e) => setReviewNotes(e.target.value)}
            placeholder="Notes for the review team..."
            rows={4}
            style={{ ...inputStyle, resize: 'vertical' }}
          />

          <button onClick={handleSave} disabled={saving} style={saveBtnStyle}>
            {saving ? 'Saving...' : 'Save Review'}
          </button>
          {saveMsg && (
            <div
              style={{
                marginTop: 6,
                fontSize: 11,
                color: saveMsg.startsWith('Error') ? '#991b1b' : '#166534',
              }}
            >
              {saveMsg}
            </div>
          )}
        </Section>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Tiny layout helpers                                               */
/* ------------------------------------------------------------------ */

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div
        style={{
          fontWeight: 700,
          fontSize: 11,
          color: '#9ca3af',
          textTransform: 'uppercase',
          marginBottom: 6,
          letterSpacing: '0.04em',
        }}
      >
        {title}
      </div>
      {children}
    </div>
  );
}

function MetaRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        padding: '2px 0',
        gap: 8,
      }}
    >
      <span style={{ color: '#6b7280', flexShrink: 0 }}>{label}</span>
      <span
        style={{
          fontWeight: 500,
          textAlign: 'right',
          wordBreak: 'break-all',
          ...(mono ? { fontFamily: 'monospace', fontSize: 11 } : {}),
        }}
      >
        {value}
      </span>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Shared inline styles                                              */
/* ------------------------------------------------------------------ */

const linkBtnStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  color: '#2563eb',
  cursor: 'pointer',
  fontSize: 13,
  padding: 0,
  textDecoration: 'none',
};

const labelStyle: React.CSSProperties = {
  display: 'block',
  fontWeight: 600,
  color: '#374151',
  marginTop: 8,
  marginBottom: 3,
  fontSize: 12,
};

const inputStyle: React.CSSProperties = {
  width: '100%',
  padding: '6px 8px',
  border: '1px solid #d1d5db',
  borderRadius: 4,
  fontSize: 12,
  fontFamily: 'inherit',
  boxSizing: 'border-box',
};

const saveBtnStyle: React.CSSProperties = {
  marginTop: 10,
  width: '100%',
  padding: '7px 0',
  background: '#2563eb',
  color: '#fff',
  border: 'none',
  borderRadius: 4,
  fontSize: 13,
  fontWeight: 600,
  cursor: 'pointer',
};
