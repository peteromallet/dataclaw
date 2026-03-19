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

const PRESET_RULES: { label: string; type: string; value: string; reason: string }[] = [
  { label: 'Your Company Domain', type: 'redact_string', value: '@yourcompany.com', reason: 'Redact company email domain' },
  { label: 'Internal Domains', type: 'block_domain', value: '*.internal', reason: 'Block internal domain references' },
  { label: 'Company Name', type: 'redact_string', value: 'YourCompanyName', reason: 'Redact company name from traces' },
  { label: 'Slack Workspace URL', type: 'redact_string', value: 'yourteam.slack.com', reason: 'Redact Slack workspace URL' },
];

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
      <h2 style={{ margin: '0 0 4px', fontSize: '20px', fontWeight: 600, color: '#111' }}>Rules</h2>
      <p style={{ fontSize: 13, color: '#6b7280', margin: '0 0 20px 0' }}>Configure redaction and exclusion filters</p>

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

      {/* Built-in redaction note */}
      <div
        style={{
          background: '#f0fdf4',
          border: '1px solid #bbf7d0',
          borderRadius: '8px',
          padding: '14px 20px',
          marginBottom: '24px',
          fontSize: '13px',
          lineHeight: 1.6,
          color: '#166534',
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: 4 }}>Built-in redaction (always active)</div>
        <div style={{ color: '#15803d' }}>
          DataClaw automatically redacts API keys (OpenAI, Anthropic, AWS, GitHub, HuggingFace, npm, PyPI, Slack),
          JWTs, database URLs, bearer tokens, private keys, emails, IP addresses, and high-entropy secrets.
          Use the rules below to add <strong>your own</strong> patterns — company names, internal URLs, team-specific strings.
        </div>
      </div>

      {/* Suggested Rules */}
      {(() => {
        const existingValues = new Set(policies.map(p => p.value));
        const availablePresets = PRESET_RULES.filter(p => !existingValues.has(p.value));
        if (loading || availablePresets.length === 0) return null;
        return (
        <div
          style={{
            background: '#fffbeb',
            border: '1px solid #fde68a',
            borderRadius: '8px',
            padding: '16px 20px',
            marginBottom: '24px',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <h3 style={{ margin: 0, fontSize: '14px', fontWeight: 600, color: '#92400e' }}>
              Suggested Rules
            </h3>
            <button
              onClick={async () => {
                try {
                  for (const preset of availablePresets) {
                    await api.policies.add(preset.type, preset.value, preset.reason);
                  }
                  await loadPolicies();
                } catch (e: unknown) {
                  setError(e instanceof Error ? e.message : 'Failed to add presets');
                }
              }}
              style={{
                padding: '5px 14px',
                background: '#92400e',
                color: '#fff',
                border: 'none',
                borderRadius: 5,
                fontSize: 12,
                fontWeight: 500,
                cursor: 'pointer',
                whiteSpace: 'nowrap',
              }}
            >
              Add All
            </button>
          </div>
          <p style={{ fontSize: 12, color: '#92400e', margin: '0 0 12px' }}>
            Common redaction patterns you can add:
          </p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {availablePresets.map((preset) => (
              <div
                key={preset.label}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 12,
                  padding: '8px 12px',
                  background: '#fff',
                  borderRadius: 6,
                  border: '1px solid #fde68a',
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: '#374151' }}>{preset.label}</div>
                  <div style={{ fontSize: 11, color: '#6b7280', fontFamily: 'monospace', marginTop: 2 }}>
                    {TYPE_LABELS[preset.type]} &middot; {preset.value}
                  </div>
                </div>
                <button
                  onClick={async () => {
                    try {
                      await api.policies.add(preset.type, preset.value, preset.reason);
                      await loadPolicies();
                    } catch (e: unknown) {
                      setError(e instanceof Error ? e.message : 'Failed to add preset');
                    }
                  }}
                  style={{
                    padding: '5px 14px',
                    background: '#2563eb',
                    color: '#fff',
                    border: 'none',
                    borderRadius: 5,
                    fontSize: 12,
                    fontWeight: 500,
                    cursor: 'pointer',
                    whiteSpace: 'nowrap',
                  }}
                >
                  Add
                </button>
              </div>
            ))}
          </div>
        </div>
        );
      })()}

      {/* Policies Table */}
      {loading ? (
        <p style={{ color: '#6b7280', fontSize: '14px' }}>Loading...</p>
      ) : policies.length === 0 ? (
        null
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
