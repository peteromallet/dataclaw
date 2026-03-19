import { useState, useEffect } from 'react';
import type { Bundle, Session } from '../types.ts';
import { api } from '../api.ts';
import { BadgeChip } from '../components/BadgeChip.tsx';

export function Bundles() {
  const [bundles, setBundles] = useState<Bundle[]>([]);
  const [creating, setCreating] = useState(false);
  const [approvedSessions, setApprovedSessions] = useState<Session[]>([]);
  const [excludedIds, setExcludedIds] = useState<Set<string>>(new Set());
  const [note, setNote] = useState('');
  const [attestation, setAttestation] = useState('');
  const [exportResults, setExportResults] = useState<Record<string, string>>({});
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const includedSessions = approvedSessions.filter(s => !excludedIds.has(s.session_id));

  function toggleSession(id: string) {
    setExcludedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    if (excludedIds.size === 0) {
      setExcludedIds(new Set(approvedSessions.map(s => s.session_id)));
    } else {
      setExcludedIds(new Set());
    }
  }

  async function loadBundles() {
    try {
      const data = await api.bundles.list();
      setBundles(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load bundles');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadBundles();
  }, []);

  async function startCreating() {
    setCreating(true);
    setError(null);
    try {
      const sessions = await api.sessions.list({ status: 'approved', limit: 500 });
      setApprovedSessions(sessions);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load approved sessions');
    }
  }

  function sourceDist(sessions: Session[]): Record<string, number> {
    const dist: Record<string, number> = {};
    for (const s of sessions) {
      dist[s.source] = (dist[s.source] || 0) + 1;
    }
    return dist;
  }

  function projectDist(sessions: Session[]): Record<string, number> {
    const dist: Record<string, number> = {};
    for (const s of sessions) {
      dist[s.project] = (dist[s.project] || 0) + 1;
    }
    return dist;
  }

  async function handleCreate() {
    if (includedSessions.length === 0) return;
    setError(null);
    try {
      const ids = includedSessions.map((s) => s.session_id);
      await api.bundles.create(ids, note || undefined, attestation || undefined);
      setCreating(false);
      setNote('');
      setAttestation('');
      setApprovedSessions([]);
      setExcludedIds(new Set());
      setLoading(true);
      await loadBundles();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to create bundle');
    }
  }

  async function handleExport(bundleId: string) {
    setError(null);
    try {
      const result = await api.bundles.export(bundleId);
      setExportResults((prev) => ({ ...prev, [bundleId]: result.export_path }));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Export failed');
    }
  }

  async function toggleExpand(bundleId: string) {
    if (expandedId === bundleId) {
      setExpandedId(null);
      return;
    }
    try {
      const detail = await api.bundles.get(bundleId);
      setBundles((prev) =>
        prev.map((b) => (b.bundle_id === bundleId ? { ...b, sessions: detail.sessions } : b)),
      );
      setExpandedId(bundleId);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load bundle detail');
    }
  }

  function truncateId(id: string): string {
    return id.length > 12 ? id.slice(0, 12) + '...' : id;
  }

  const [approvedCount, setApprovedCount] = useState(0);

  useEffect(() => {
    api.stats().then((s) => setApprovedCount(s.by_status['approved'] ?? 0)).catch(() => {});
  }, []);

  return (
    <div style={{ padding: '24px', maxWidth: '960px', margin: '0 auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '4px' }}>
        <h2 style={{ margin: 0, fontSize: '20px', fontWeight: 600, color: '#111' }}>Exports</h2>
        {!creating && (
          <button
            onClick={startCreating}
            style={{
              padding: '8px 16px',
              background: '#2563eb',
              color: '#fff',
              border: 'none',
              borderRadius: '6px',
              fontSize: '13px',
              fontWeight: 500,
              cursor: 'pointer',
            }}
          >
            New Bundle
          </button>
        )}
      </div>
      <p style={{ fontSize: 13, color: '#6b7280', margin: '0 0 16px 0' }}>Package approved sessions for upload</p>

      {/* Approved session CTA */}
      {!creating && approvedCount > 0 && (
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '12px 16px',
          marginBottom: 16,
          background: '#f0fdf4',
          border: '1px solid #bbf7d0',
          borderRadius: 8,
          fontSize: 13,
          color: '#166534',
        }}>
          <span>You have <strong>{approvedCount}</strong> approved session{approvedCount !== 1 ? 's' : ''} ready to bundle.</span>
          <button
            onClick={startCreating}
            style={{
              marginLeft: 'auto',
              padding: '6px 14px',
              background: '#16a34a',
              color: '#fff',
              border: 'none',
              borderRadius: 6,
              fontSize: 12,
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            Create Export
          </button>
        </div>
      )}

      {error && (
        <div
          style={{
            padding: '10px 14px',
            marginBottom: '16px',
            background: '#fee2e2',
            color: '#991b1b',
            borderRadius: '6px',
            fontSize: '13px',
          }}
        >
          {error}
        </div>
      )}

      {creating && (
        <div
          style={{
            background: '#fff',
            border: '1px solid #e5e7eb',
            borderRadius: '8px',
            padding: '20px',
            marginBottom: '24px',
          }}
        >
          <h3 style={{ margin: '0 0 16px', fontSize: '16px', fontWeight: 600 }}>Create New Bundle</h3>

          {approvedSessions.length === 0 ? (
            <p style={{ color: '#6b7280', fontSize: '14px' }}>No approved sessions available.</p>
          ) : (
            <>
              <div
                style={{
                  background: '#f9fafb',
                  borderRadius: '6px',
                  padding: '14px',
                  marginBottom: '16px',
                  fontSize: '13px',
                  lineHeight: '1.7',
                }}
              >
                <div>
                  <strong>Sessions:</strong> {includedSessions.length} of {approvedSessions.length} selected
                </div>
                <div>
                  <strong>Sources:</strong>{' '}
                  {Object.entries(sourceDist(includedSessions))
                    .map(([k, v]) => `${k} (${v})`)
                    .join(', ') || '--'}
                </div>
                <div>
                  <strong>Projects:</strong>{' '}
                  {Object.entries(projectDist(includedSessions))
                    .map(([k, v]) => `${k} (${v})`)
                    .join(', ') || '--'}
                </div>
              </div>

              {/* Session selection table */}
              <div
                style={{
                  maxHeight: 260,
                  overflowY: 'auto',
                  border: '1px solid #e5e7eb',
                  borderRadius: '6px',
                  marginBottom: '16px',
                }}
              >
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
                  <thead>
                    <tr style={{ textAlign: 'left', color: '#6b7280', borderBottom: '1px solid #e5e7eb', background: '#f9fafb', position: 'sticky', top: 0 }}>
                      <th style={{ padding: '6px 8px', fontWeight: 500, width: 32 }}>
                        <input
                          type="checkbox"
                          checked={excludedIds.size === 0}
                          onChange={toggleAll}
                          style={{ cursor: 'pointer' }}
                        />
                      </th>
                      <th style={{ padding: '6px 8px', fontWeight: 500 }}>Session</th>
                      <th style={{ padding: '6px 8px', fontWeight: 500 }}>Project</th>
                      <th style={{ padding: '6px 8px', fontWeight: 500 }}>Score</th>
                      <th style={{ padding: '6px 8px', fontWeight: 500 }}>Source</th>
                      <th style={{ padding: '6px 8px', fontWeight: 500 }}>Msgs</th>
                      <th style={{ padding: '6px 8px', fontWeight: 500 }}>Tokens</th>
                    </tr>
                  </thead>
                  <tbody>
                    {approvedSessions.map((s) => {
                      const included = !excludedIds.has(s.session_id);
                      return (
                        <tr
                          key={s.session_id}
                          onClick={() => toggleSession(s.session_id)}
                          style={{
                            borderBottom: '1px solid #f3f4f6',
                            cursor: 'pointer',
                            opacity: included ? 1 : 0.45,
                          }}
                        >
                          <td style={{ padding: '6px 8px' }}>
                            <input
                              type="checkbox"
                              checked={included}
                              onChange={() => toggleSession(s.session_id)}
                              onClick={(e) => e.stopPropagation()}
                              style={{ cursor: 'pointer' }}
                            />
                          </td>
                          <td
                            style={{ padding: '6px 8px', fontFamily: 'monospace', color: '#2563eb' }}
                            title={s.session_id}
                          >
                            {s.display_title || truncateId(s.session_id)}
                          </td>
                          <td style={{ padding: '6px 8px', color: '#374151' }}>{s.project}</td>
                          <td style={{ padding: '6px 8px', textAlign: 'center' }}>
                            {s.ai_quality_score != null ? (
                              <span style={{
                                display: 'inline-block',
                                width: 22, height: 22, lineHeight: '22px',
                                borderRadius: 4,
                                fontSize: 11, fontWeight: 700,
                                textAlign: 'center',
                                background: s.ai_quality_score >= 4 ? '#dcfce7'
                                  : s.ai_quality_score === 3 ? '#fef3c7' : '#fee2e2',
                                color: s.ai_quality_score >= 4 ? '#166534'
                                  : s.ai_quality_score === 3 ? '#92400e' : '#991b1b',
                              }}>
                                {s.ai_quality_score}
                              </span>
                            ) : (
                              <span style={{ color: '#d1d5db' }}>--</span>
                            )}
                          </td>
                          <td style={{ padding: '6px 8px', color: '#6b7280' }}>{s.source}</td>
                          <td style={{ padding: '6px 8px', color: '#374151' }}>
                            {s.user_messages + s.assistant_messages}
                          </td>
                          <td style={{ padding: '6px 8px', color: '#374151' }}>
                            {(s.input_tokens + s.output_tokens).toLocaleString()}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              <div style={{
                background: '#f0f9ff', border: '1px solid #bfdbfe', borderRadius: 6,
                padding: 14, marginBottom: 16, fontSize: 12, lineHeight: 1.7, color: '#1e40af',
              }}>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>What's in this bundle:</div>
                <div>Anonymized conversation transcripts, session metadata (tokens, duration, model), redacted content (secrets and PII removed)</div>
                <div style={{ marginTop: 6, fontWeight: 600, marginBottom: 4 }}>What's NOT included:</div>
                <div>Your file contents or source code, your reviewer notes or comments, sessions you didn't approve</div>
                <div style={{ marginTop: 8, fontSize: 11, color: '#6b7280' }}>
                  Exporting to disk keeps everything local. Only pushing to Hugging Face shares data publicly.
                </div>
              </div>

              <div style={{ marginBottom: '12px' }}>
                <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '4px', color: '#374151' }}>
                  Submission Note
                </label>
                <input
                  type="text"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder="Describe this bundle..."
                  style={{
                    width: '100%',
                    padding: '8px 10px',
                    border: '1px solid #d1d5db',
                    borderRadius: '6px',
                    fontSize: '13px',
                    boxSizing: 'border-box',
                  }}
                />
              </div>

              <div style={{ marginBottom: '16px' }}>
                <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '4px', color: '#374151' }}>
                  Attestation
                </label>
                <input
                  type="text"
                  value={attestation}
                  onChange={(e) => setAttestation(e.target.value)}
                  placeholder="I attest that..."
                  style={{
                    width: '100%',
                    padding: '8px 10px',
                    border: '1px solid #d1d5db',
                    borderRadius: '6px',
                    fontSize: '13px',
                    boxSizing: 'border-box',
                  }}
                />
              </div>
            </>
          )}

          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              onClick={handleCreate}
              disabled={includedSessions.length === 0}
              style={{
                padding: '8px 16px',
                background: includedSessions.length === 0 ? '#9ca3af' : '#16a34a',
                color: '#fff',
                border: 'none',
                borderRadius: '6px',
                fontSize: '13px',
                fontWeight: 500,
                cursor: includedSessions.length === 0 ? 'default' : 'pointer',
              }}
            >
              Create Bundle ({includedSessions.length})
            </button>
            <button
              onClick={() => {
                setCreating(false);
                setApprovedSessions([]);
                setExcludedIds(new Set());
                setNote('');
                setAttestation('');
              }}
              style={{
                padding: '8px 16px',
                background: '#fff',
                color: '#374151',
                border: '1px solid #d1d5db',
                borderRadius: '6px',
                fontSize: '13px',
                fontWeight: 500,
                cursor: 'pointer',
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {loading ? (
        <p style={{ color: '#6b7280', fontSize: '14px' }}>Loading...</p>
      ) : bundles.length === 0 ? (
        <p style={{ color: '#6b7280', fontSize: '14px' }}>No bundles yet. Create one from approved sessions.</p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {bundles.map((bundle) => (
            <div
              key={bundle.bundle_id}
              style={{
                background: '#fff',
                border: '1px solid #e5e7eb',
                borderRadius: '8px',
                overflow: 'hidden',
              }}
            >
              <div
                onClick={() => toggleExpand(bundle.bundle_id)}
                style={{
                  padding: '16px 20px',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '16px',
                  flexWrap: 'wrap',
                }}
              >
                <span
                  style={{
                    fontFamily: 'monospace',
                    fontSize: '13px',
                    color: '#2563eb',
                    fontWeight: 500,
                    minWidth: '110px',
                  }}
                  title={bundle.bundle_id}
                >
                  {truncateId(bundle.bundle_id)}
                </span>
                <span style={{ fontSize: '12px', color: '#6b7280' }}>
                  {new Date(bundle.created_at).toLocaleDateString()}
                </span>
                <span style={{ fontSize: '13px', color: '#374151' }}>
                  {bundle.session_count} session{bundle.session_count !== 1 ? 's' : ''}
                </span>
                <BadgeChip kind="status" value={bundle.status} />
                {bundle.submission_note && (
                  <span style={{ fontSize: '13px', color: '#6b7280', fontStyle: 'italic', flex: 1 }}>
                    {bundle.submission_note}
                  </span>
                )}
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    handleExport(bundle.bundle_id);
                  }}
                  style={{
                    marginLeft: 'auto',
                    padding: '5px 12px',
                    background: '#f3f4f6',
                    color: '#374151',
                    border: '1px solid #d1d5db',
                    borderRadius: '5px',
                    fontSize: '12px',
                    fontWeight: 500,
                    cursor: 'pointer',
                    whiteSpace: 'nowrap',
                  }}
                >
                  Export to Disk
                </button>
              </div>

              {exportResults[bundle.bundle_id] && (
                <div
                  style={{
                    padding: '8px 20px',
                    background: '#f0fdf4',
                    borderTop: '1px solid #e5e7eb',
                    fontSize: '12px',
                    color: '#166534',
                    fontFamily: 'monospace',
                  }}
                >
                  Exported to: {exportResults[bundle.bundle_id]}
                </div>
              )}

              {expandedId === bundle.bundle_id && bundle.sessions && (
                <div style={{ borderTop: '1px solid #e5e7eb', padding: '12px 20px' }}>
                  {bundle.sessions.length === 0 ? (
                    <p style={{ color: '#6b7280', fontSize: '13px', margin: 0 }}>No sessions in this bundle.</p>
                  ) : (
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
                      <thead>
                        <tr style={{ textAlign: 'left', color: '#6b7280', borderBottom: '1px solid #e5e7eb' }}>
                          <th style={{ padding: '6px 8px', fontWeight: 500 }}>Session</th>
                          <th style={{ padding: '6px 8px', fontWeight: 500 }}>Project</th>
                          <th style={{ padding: '6px 8px', fontWeight: 500 }}>Score</th>
                          <th style={{ padding: '6px 8px', fontWeight: 500 }}>Source</th>
                          <th style={{ padding: '6px 8px', fontWeight: 500 }}>Messages</th>
                          <th style={{ padding: '6px 8px', fontWeight: 500 }}>Tokens</th>
                        </tr>
                      </thead>
                      <tbody>
                        {bundle.sessions.map((s) => (
                          <tr key={s.session_id} style={{ borderBottom: '1px solid #f3f4f6' }}>
                            <td
                              style={{ padding: '6px 8px', fontFamily: 'monospace', color: '#2563eb' }}
                              title={s.session_id}
                            >
                              {truncateId(s.session_id)}
                            </td>
                            <td style={{ padding: '6px 8px', color: '#374151' }}>{s.project}</td>
                            <td style={{ padding: '6px 8px', textAlign: 'center' }}>
                              {s.ai_quality_score != null ? (
                                <span style={{
                                  display: 'inline-block',
                                  width: 22, height: 22, lineHeight: '22px',
                                  borderRadius: 4,
                                  fontSize: 11, fontWeight: 700,
                                  textAlign: 'center',
                                  background: s.ai_quality_score >= 4 ? '#dcfce7'
                                    : s.ai_quality_score === 3 ? '#fef3c7' : '#fee2e2',
                                  color: s.ai_quality_score >= 4 ? '#166534'
                                    : s.ai_quality_score === 3 ? '#92400e' : '#991b1b',
                                }}>
                                  {s.ai_quality_score}
                                </span>
                              ) : (
                                <span style={{ color: '#d1d5db' }}>--</span>
                              )}
                            </td>
                            <td style={{ padding: '6px 8px', color: '#6b7280' }}>{s.source}</td>
                            <td style={{ padding: '6px 8px', color: '#374151' }}>
                              {s.user_messages + s.assistant_messages}
                            </td>
                            <td style={{ padding: '6px 8px', color: '#374151' }}>
                              {(s.input_tokens + s.output_tokens).toLocaleString()}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
