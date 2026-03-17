import type { Session, SessionDetail, Bundle, Policy, Stats, ProjectSummary } from './types.ts';

const BASE = '/api';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${res.status}`);
  }
  return res.json();
}

function qs(params: Record<string, string | number | null | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== '') p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : '';
}

export const api = {
  sessions: {
    list(params: {
      status?: string | null;
      source?: string | null;
      project?: string | null;
      q?: string | null;
      sort?: string;
      order?: string;
      limit?: number;
      offset?: number;
    } = {}): Promise<Session[]> {
      return request(`/sessions${qs(params)}`);
    },

    get(id: string): Promise<SessionDetail> {
      return request(`/sessions/${encodeURIComponent(id)}`);
    },

    update(id: string, body: { status?: string; notes?: string; reason?: string }): Promise<{ ok: boolean }> {
      return request(`/sessions/${encodeURIComponent(id)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    },
  },

  search(q: string, limit = 50, offset = 0): Promise<Session[]> {
    return request(`/search${qs({ q, limit, offset })}`);
  },

  stats(): Promise<Stats> {
    return request('/stats');
  },

  projects(): Promise<ProjectSummary[]> {
    return request('/projects');
  },

  bundles: {
    list(): Promise<Bundle[]> {
      return request('/bundles');
    },

    get(id: string): Promise<Bundle> {
      return request(`/bundles/${encodeURIComponent(id)}`);
    },

    create(sessionIds: string[], note?: string, attestation?: string): Promise<{ bundle_id: string }> {
      return request('/bundles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_ids: sessionIds, note, attestation }),
      });
    },

    export(id: string): Promise<{ ok: boolean; export_path: string; session_count: number }> {
      return request(`/bundles/${encodeURIComponent(id)}/export`, { method: 'POST' });
    },
  },

  policies: {
    list(): Promise<Policy[]> {
      return request('/policies');
    },

    add(policyType: string, value: string, reason?: string): Promise<{ policy_id: string }> {
      return request('/policies', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ policy_type: policyType, value, reason }),
      });
    },

    remove(id: string): Promise<{ ok: boolean }> {
      return request(`/policies/${encodeURIComponent(id)}`, { method: 'DELETE' });
    },
  },

  scan(): Promise<{ ok: boolean; new_sessions: Record<string, number> }> {
    return request('/scan', { method: 'POST' });
  },
};
