import { useState, useEffect } from 'react';
import type { Policy } from '../types.ts';
import { api } from '../api.ts';

const POLICY_TYPE_OPTIONS: { label: string; value: string }[] = [
  { label: 'Redact String', value: 'redact_string' },
  { label: 'Redact Username', value: 'redact_username' },
  { label: 'Exclude Project', value: 'exclude_project' },
  { label: 'Block Domain', value: 'block_domain' },
];

const TYPE_LABELS: Record<string, string> = {
  redact_string: 'Redact String',
  redact_username: 'Redact Username',
  exclude_project: 'Exclude Project',
  block_domain: 'Block Domain',
};

export function Policies() {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [newType, setNewType] = useState('redact_string');
  const [newValue, setNewValue] = useState('');
  const [newReason, setNewReason] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function loadPolicies() {
    try {
      const data = await api.policies.list();
      setPolicies(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load policies');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadPolicies();
  }, []);

  async function handleAdd() {
    if (!newValue.trim()) return;
    setError(null);
    try {
      await api.policies.add(newType, newValue.trim(), newReason.trim() || undefined);
      setNewValue('');
      setNewReason('');
      await loadPolicies();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to add policy');
    }
  }

  async function handleDelete(id: string) {
    setError(null);
    try {
      await api.policies.remove(id);
      await loadPolicies();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to delete policy');
    }
  }

  return (
    <div style={{ padding: '24px', maxWidth: '960px', margin: '0 auto' }}>
      <h2 style={{ margin: '0 0 24px', fontSize: '20px', fontWeight: 600, color: '#111' }}>Policies</h2>

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

      {/* Add Policy Form */}
      <div
        style={{
          background: '#fff',
          border: '1px solid #e5e7eb',
          borderRadius: '8px',
          padding: '16px 20px',
          marginBottom: '24px',
        }}
      >
        <h3 style={{ margin: '0 0 12px', fontSize: '14px', fontWeight: 600, color: '#374151' }}>Add Policy</h3>
        <div style={{ display: 'flex', gap: '10px', alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            <label style={{ fontSize: '12px', fontWeight: 500, color: '#6b7280' }}>Type</label>
            <select
              value={newType}
              onChange={(e) => setNewType(e.target.value)}
              style={{
                padding: '8px 10px',
                border: '1px solid #d1d5db',
                borderRadius: '6px',
                fontSize: '13px',
                background: '#fff',
                minWidth: '160px',
              }}
            >
              {POLICY_TYPE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', flex: 1, minWidth: '180px' }}>
            <label style={{ fontSize: '12px', fontWeight: 500, color: '#6b7280' }}>Value</label>
            <input
              type="text"
              value={newValue}
              onChange={(e) => setNewValue(e.target.value)}
              placeholder="String, username, project, or domain..."
              style={{
                padding: '8px 10px',
                border: '1px solid #d1d5db',
                borderRadius: '6px',
                fontSize: '13px',
                boxSizing: 'border-box',
              }}
            />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', flex: 1, minWidth: '140px' }}>
            <label style={{ fontSize: '12px', fontWeight: 500, color: '#6b7280' }}>Reason (optional)</label>
            <input
              type="text"
              value={newReason}
              onChange={(e) => setNewReason(e.target.value)}
              placeholder="Why this policy exists..."
              style={{
                padding: '8px 10px',
                border: '1px solid #d1d5db',
                borderRadius: '6px',
                fontSize: '13px',
                boxSizing: 'border-box',
              }}
            />
          </div>
          <button
            onClick={handleAdd}
            disabled={!newValue.trim()}
            style={{
              padding: '8px 16px',
              background: newValue.trim() ? '#2563eb' : '#9ca3af',
              color: '#fff',
              border: 'none',
              borderRadius: '6px',
              fontSize: '13px',
              fontWeight: 500,
              cursor: newValue.trim() ? 'pointer' : 'default',
              whiteSpace: 'nowrap',
            }}
          >
            Add
          </button>
        </div>
      </div>

      {/* Policies Table */}
      {loading ? (
        <p style={{ color: '#6b7280', fontSize: '14px' }}>Loading...</p>
      ) : policies.length === 0 ? (
        <p style={{ color: '#6b7280', fontSize: '14px' }}>No policies configured.</p>
      ) : (
        <div
          style={{
            background: '#fff',
            border: '1px solid #e5e7eb',
            borderRadius: '8px',
            overflow: 'hidden',
          }}
        >
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
            <thead>
              <tr
                style={{
                  textAlign: 'left',
                  background: '#f9fafb',
                  borderBottom: '1px solid #e5e7eb',
                }}
              >
                <th style={{ padding: '10px 16px', fontWeight: 600, color: '#374151' }}>Type</th>
                <th style={{ padding: '10px 16px', fontWeight: 600, color: '#374151' }}>Value</th>
                <th style={{ padding: '10px 16px', fontWeight: 600, color: '#374151' }}>Reason</th>
                <th style={{ padding: '10px 16px', fontWeight: 600, color: '#374151' }}>Created</th>
                <th style={{ padding: '10px 16px', fontWeight: 600, color: '#374151', width: '80px' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {policies.map((policy, i) => (
                <tr
                  key={policy.policy_id}
                  style={{
                    borderBottom: '1px solid #e5e7eb',
                    background: i % 2 === 0 ? '#fff' : '#f9fafb',
                  }}
                >
                  <td style={{ padding: '10px 16px', color: '#374151', fontWeight: 500 }}>
                    {TYPE_LABELS[policy.policy_type] ?? policy.policy_type}
                  </td>
                  <td style={{ padding: '10px 16px', fontFamily: 'monospace', color: '#111', fontSize: '12px' }}>
                    {policy.value}
                  </td>
                  <td style={{ padding: '10px 16px', color: '#6b7280' }}>
                    {policy.reason || '\u2014'}
                  </td>
                  <td style={{ padding: '10px 16px', color: '#6b7280', fontSize: '12px' }}>
                    {new Date(policy.created_at).toLocaleDateString()}
                  </td>
                  <td style={{ padding: '10px 16px' }}>
                    <button
                      onClick={() => handleDelete(policy.policy_id)}
                      style={{
                        padding: '4px 10px',
                        background: '#fee2e2',
                        color: '#991b1b',
                        border: '1px solid #fca5a5',
                        borderRadius: '5px',
                        fontSize: '12px',
                        fontWeight: 500,
                        cursor: 'pointer',
                      }}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
