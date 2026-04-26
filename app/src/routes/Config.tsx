import { useEffect, useMemo, useRef, useState } from "react";
import {
  dataclawConfigGet,
  dataclawConfigSet,
  dataclawListProjects,
  hfWhoami
} from "../lib/ipc";
import type { ConfigSetArgs } from "../lib/ipc";
import type { DataClawConfig, Project } from "../lib/types";
import { SOURCE_CHOICES } from "../lib/types";

const PROJECT_CACHE_KEY = "dataclaw.projects.v1";
const PROJECT_CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const DEFAULT_DATASET_NAME = "my-dataclaw-data";

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

function joinList(value: string[] | undefined | null) {
  if (!value || !Array.isArray(value)) return "";
  return value.join(", ");
}

function baseName(name: string) {
  return name.includes(":") ? name.split(":").slice(1).join(":") : name;
}

type ProjectGroup = {
  base: string;
  members: Project[];
  sources: string[];
  totalSessions: number;
};

type ProjectCache = {
  scannedAt: number;
  projects: Project[];
};

function readProjectCache(): ProjectCache | null {
  try {
    const raw = window.localStorage.getItem(PROJECT_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<ProjectCache>;
    if (typeof parsed.scannedAt !== "number" || !Array.isArray(parsed.projects)) return null;
    return { scannedAt: parsed.scannedAt, projects: parsed.projects };
  } catch {
    return null;
  }
}

function writeProjectCache(projects: Project[]) {
  window.localStorage.setItem(
    PROJECT_CACHE_KEY,
    JSON.stringify({ scannedAt: Date.now(), projects })
  );
}

function excludedFromConfig(cfg: DataClawConfig) {
  const initial = new Set<string>();
  const fromCfg = (cfg.excluded_projects ?? cfg.exclude) as string[] | undefined;
  if (Array.isArray(fromCfg)) fromCfg.forEach((name) => initial.add(name));
  return initial;
}

function groupProjects(projects: Project[]): ProjectGroup[] {
  const groups = new Map<string, ProjectGroup>();
  for (const project of projects) {
    const base = baseName(project.name);
    let group = groups.get(base);
    if (!group) {
      group = { base, members: [], sources: [], totalSessions: 0 };
      groups.set(base, group);
    }
    group.members.push(project);
    if (!group.sources.includes(project.source)) group.sources.push(project.source);
    group.totalSessions += project.sessions;
  }
  return Array.from(groups.values()).sort((a, b) => a.base.localeCompare(b.base));
}

function hoursToDays(hours: number | undefined) {
  if (typeof hours !== "number" || !Number.isFinite(hours) || hours <= 0) return "1";
  return String(Math.max(1, Math.round(hours / 24)));
}

function daysToHours(days: string) {
  const parsed = Number(days);
  const safeDays = Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
  return Math.round(safeDays * 24);
}

function repoNameFromConfig(repo: unknown) {
  if (typeof repo !== "string" || !repo.trim()) return DEFAULT_DATASET_NAME;
  const trimmed = repo.trim();
  const name = trimmed.includes("/") ? trimmed.split("/").pop() || DEFAULT_DATASET_NAME : trimmed;
  const normalized = name.trim().toLowerCase();
  return normalized === "my-personal-codex-data" ? DEFAULT_DATASET_NAME : normalized;
}

function targetRepo(user: string | null, repoName: string) {
  const name = (repoName.trim() || DEFAULT_DATASET_NAME).toLowerCase();
  return user ? `${user}/${name}` : "";
}

export default function Config() {
  const [config, setConfig] = useState<DataClawConfig | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const [repo, setRepo] = useState(DEFAULT_DATASET_NAME);
  const [source, setSource] = useState("all");
  const [redact, setRedact] = useState("");
  const [redactUsernames, setRedactUsernames] = useState("");
  const [defaultBucket, setDefaultBucket] = useState("");
  const [privacyFilter, setPrivacyFilter] = useState(true);
  const [privacyFilterDevice, setPrivacyFilterDevice] = useState("auto");
  const [launchAtLogin, setLaunchAtLogin] = useState(true);
  const [syncEnabled, setSyncEnabled] = useState(true);
  const [syncIntervalDays, setSyncIntervalDays] = useState("1");
  const [hfUser, setHfUser] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const saveTimer = useRef<number | null>(null);
  const latestSaveId = useRef(0);
  const loadedOnce = useRef(false);

  function applyProjects(projs: Project[], cfg: DataClawConfig) {
    const initial = excludedFromConfig(cfg);
    setProjects(
      projs.map((project) => ({
        ...project,
        excluded: initial.has(project.name)
      }))
    );
    setExcluded(initial);
  }

  async function loadProjects(cfg: DataClawConfig, force = false) {
    const cached = readProjectCache();
    if (!force && cached && Date.now() - cached.scannedAt < PROJECT_CACHE_TTL_MS) {
      applyProjects(cached.projects, cfg);
      setProjectsLoading(false);
      return;
    }

    setProjectsLoading(true);
    let projs: Project[] = [];
    try {
      projs = await dataclawListProjects("all");
      writeProjectCache(projs);
    } catch (err) {
      setError(`Project scan failed: ${errorMessage(err)}`);
      if (cached) projs = cached.projects;
    }
    applyProjects(projs, cfg);
    setProjectsLoading(false);
  }

  async function loadAll(forceProjectScan = false) {
    setLoading(true);
    setProjectsLoading(true);
    setError("");
    try {
      const [cfg, whoami] = await Promise.all([
        dataclawConfigGet({ showSecrets: true }),
        hfWhoami().catch(() => null)
      ]);
      const user = whoami && typeof whoami.user === "string" ? whoami.user : null;
      setHfUser(user);
      setConfig(cfg);
      setRepo(repoNameFromConfig(cfg.repo));
      setSource(typeof cfg.source === "string" && cfg.source !== "auto" ? cfg.source : "all");
      setRedact(joinList(cfg.redact_strings ?? cfg.redact));
      setRedactUsernames(joinList(cfg.redact_usernames));
      setDefaultBucket(
        typeof cfg.folder_rules?.default_bucket === "string"
          ? cfg.folder_rules.default_bucket
          : typeof cfg.default_bucket === "string"
            ? cfg.default_bucket
            : ""
      );
      setPrivacyFilter(cfg.privacy_filter?.enabled !== false);
      setPrivacyFilterDevice(
        typeof cfg.privacy_filter?.device === "string"
          ? cfg.privacy_filter.device
          : "auto"
      );
      setLaunchAtLogin(cfg.app?.launch_at_login !== false);
      setSyncEnabled(cfg.app?.sync_enabled !== false);
      setSyncIntervalDays(hoursToDays(cfg.app?.sync_interval_hours));
      setLoading(false);
      loadedOnce.current = true;
      await loadProjects(cfg, forceProjectScan);
    } catch (err) {
      setError(errorMessage(err));
      setLoading(false);
      setProjectsLoading(false);
    }
  }

  useEffect(() => {
    void loadAll();
  }, []);

  function toggleGroup(group: ProjectGroup) {
    setExcluded((current) => {
      const next = new Set(current);
      const allExcluded = group.members.every((m) => next.has(m.name));
      for (const member of group.members) {
        if (allExcluded) next.delete(member.name);
        else next.add(member.name);
      }
      return next;
    });
  }

  function selectAll() {
    setExcluded(new Set());
  }

  function deselectAll() {
    setExcluded(new Set(projects.map((p) => p.name)));
  }

  async function save(silent = false) {
    setBusy(true);
    setError("");
    if (!silent) setNotice("");
    try {
      const args: ConfigSetArgs = {
        repo: targetRepo(hfUser, repo) || undefined,
        source: source || undefined,
        set_redact: redact,
        set_redact_usernames: redactUsernames,
        default_bucket: defaultBucket.trim(),
        privacy_filter: privacyFilter,
        privacy_filter_device: privacyFilterDevice,
        launch_at_login: launchAtLogin,
        sync_enabled: syncEnabled,
        sync_interval_hours: daysToHours(syncIntervalDays),
        confirm_projects: true
      };
      if (!projectsLoading) {
        args.set_excluded = Array.from(excluded).join(",");
      }
      const updated = await dataclawConfigSet(args);
      setConfig(updated);
      setNotice("Saved.");
      window.setTimeout(() => setNotice(""), silent ? 1200 : 2500);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!loadedOnce.current || loading) return;
    setNotice("Unsaved changes…");
    if (saveTimer.current !== null) window.clearTimeout(saveTimer.current);
    const saveId = latestSaveId.current + 1;
    latestSaveId.current = saveId;
    saveTimer.current = window.setTimeout(() => {
      if (saveId === latestSaveId.current) void save(true);
    }, 700);
    return () => {
      if (saveTimer.current !== null) window.clearTimeout(saveTimer.current);
    };
  }, [
    repo,
    source,
    redact,
    redactUsernames,
    defaultBucket,
    privacyFilter,
    privacyFilterDevice,
    launchAtLogin,
    syncEnabled,
    syncIntervalDays,
    excluded,
    loading,
    projectsLoading
  ]);

  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (busy || notice === "Unsaved changes…") {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [busy, notice]);

  const groups = useMemo(() => groupProjects(projects), [projects]);
  const filteredGroups = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return groups;
    return groups.filter(
      (g) =>
        g.base.toLowerCase().includes(q) ||
        g.sources.some((s) => s.toLowerCase().includes(q))
    );
  }, [filter, groups]);

  const includedCount = projects.length - excluded.size;
  const saveDestination = targetRepo(hfUser, repo);
  const saveStatus = busy ? "Saving changes…" : notice || "Changes save automatically.";

  return (
    <section className="page">
      <h1>Config</h1>
      {error ? <div className="error">{error}</div> : null}

      <div className="card">
        <h2>Hugging Face dataset</h2>
        <p className="muted">
          Where DataClaw pushes your conversations. Format: <code>username/dataset-name</code>.
          The dataset is created automatically on first push.
        </p>
        <div className="row" style={{ marginTop: 10 }}>
          <label htmlFor="repo">Repo name</label>
          <input
            id="repo"
            type="text"
            value={repo}
            onChange={(event) => setRepo(event.target.value)}
            placeholder={DEFAULT_DATASET_NAME}
          />
          <span />
          <p className="muted">
            {hfUser
              ? `This will save to ${saveDestination}.`
              : "You need to auth."}
          </p>
          <label htmlFor="source">Agent Source</label>
          <select
            id="source"
            value={source}
            onChange={(event) => setSource(event.target.value)}
          >
            {SOURCE_CHOICES.map((choice) => (
              <option key={choice} value={choice}>
                {choice}
              </option>
            ))}
          </select>
          <label htmlFor="default-bucket">Default bucket</label>
          <input
            id="default-bucket"
            type="text"
            value={defaultBucket}
            onChange={(event) => setDefaultBucket(event.target.value)}
            placeholder="(optional folder for projects without an assignment)"
          />
        </div>
      </div>

      <div className="card">
        <h2>Automation</h2>
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={launchAtLogin}
            onChange={(event) => setLaunchAtLogin(event.target.checked)}
          />
          <span>Launch DataClaw when you sign in</span>
        </label>
        <label className="checkbox-row" style={{ marginTop: 12 }}>
          <input
            type="checkbox"
            checked={syncEnabled}
            onChange={(event) => setSyncEnabled(event.target.checked)}
          />
          <span>Sync to Hugging Face automatically</span>
        </label>
        <div className="row" style={{ marginTop: 10 }}>
          <label htmlFor="sync-interval-days">Sync every</label>
          <input
            id="sync-interval-days"
            type="number"
            min={1}
            max={365}
            step={1}
            value={syncIntervalDays}
            onChange={(event) => setSyncIntervalDays(event.target.value)}
            disabled={!syncEnabled}
          />
          <span className="muted">days</span>
        </div>
      </div>

      <div className="card">
        <h2>Redaction</h2>
        <p className="muted">
          Strings (one per line or comma-separated) that DataClaw will replace before pushing.
          Add API keys, full names, internal domains, anything you don&apos;t want public.
        </p>
        <div style={{ marginTop: 10 }}>
          <label htmlFor="redact">Redact strings</label>
          <textarea
            id="redact"
            value={redact}
            onChange={(event) => setRedact(event.target.value)}
            placeholder="API_KEY_VALUE, MyFullName, internal.example.com"
            rows={3}
          />
        </div>
        <div style={{ marginTop: 10 }}>
          <label htmlFor="redact-usernames">Redact usernames</label>
          <textarea
            id="redact-usernames"
            value={redactUsernames}
            onChange={(event) => setRedactUsernames(event.target.value)}
            placeholder="github-handle, discord-name"
            rows={2}
          />
        </div>
        <label className="checkbox-row" style={{ marginTop: 12 }}>
          <input
            type="checkbox"
            checked={privacyFilter}
            onChange={(event) => setPrivacyFilter(event.target.checked)}
          />
          <span>Privacy filter</span>
        </label>
        <div className="row" style={{ marginTop: 10 }}>
          <label htmlFor="privacy-filter-device">Privacy filter device</label>
          <select
            id="privacy-filter-device"
            value={privacyFilterDevice}
            onChange={(event) => setPrivacyFilterDevice(event.target.value)}
            disabled={!privacyFilter}
          >
            <option value="auto">Auto (GPU if available)</option>
            <option value="mps">Apple GPU (MPS)</option>
            <option value="cpu">CPU</option>
          </select>
        </div>
      </div>

      <div className="card">
        <h2>Folders to include</h2>
        <p className="muted">
          {projectsLoading
            ? "Scanning…"
            : `Found ${projects.length} projects across all sources. Uncheck any you don't want pushed.`}
        </p>
        <div className="project-toolbar">
          <input
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="Filter projects…"
            style={{ flex: 1, minWidth: 180 }}
          />
          <button type="button" onClick={selectAll} disabled={busy || projectsLoading}>
            Include all
          </button>
          <button type="button" onClick={deselectAll} disabled={busy || projectsLoading}>
            Exclude all
          </button>
          <span className="count">
            {includedCount}/{projects.length} included
          </span>
        </div>
        <div className="project-list">
          {projectsLoading ? (
            <div className="empty-state">
              <span className="spinner" />
              Scanning projects… (first scan can take ~10 seconds)
            </div>
          ) : filteredGroups.length === 0 ? (
            <div className="empty-state">No projects match this filter.</div>
          ) : (
            filteredGroups.map((group) => {
              const allExcluded = group.members.every((m) => excluded.has(m.name));
              const someExcluded =
                !allExcluded && group.members.some((m) => excluded.has(m.name));
              return (
                <label key={group.base} className="project-row">
                  <input
                    type="checkbox"
                    checked={!allExcluded}
                    ref={(input) => {
                      if (input) input.indeterminate = someExcluded;
                    }}
                    onChange={() => toggleGroup(group)}
                  />
                  <span className="name" title={group.base}>
                    {group.base}
                  </span>
                  <span style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                    {group.sources.map((s) => (
                      <span key={s} className="source-pill">
                        {s}
                      </span>
                    ))}
                  </span>
                  <span className="meta">{group.totalSessions} sess</span>
                </label>
              );
            })
          )}
        </div>
      </div>

      <div className="actions">
        <button type="button" onClick={() => void loadAll(true)} disabled={busy || loading || projectsLoading}>
          Reload
        </button>
      </div>
      <div className={`floating-save ${busy || notice === "Unsaved changes…" ? "active" : ""}`} role="status">
        {saveStatus}
      </div>
    </section>
  );
}
