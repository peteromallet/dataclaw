# Ground Foundry Scientist Workbench — Progress

## What Was Built

### Phase 1: Index + Daemon Backbone (Complete)

**`dataclaw/index.py`** — SQLite + FTS5 session index
- Schema: sessions (with badge fields, review state, blob path), bundles, policies
- Contentless FTS5 for full-text search across transcripts
- Functions: upsert_sessions, query_sessions, get_session_detail, update_session, search_fts, get_stats, create_bundle, get_bundle, get_policies, add_policy, remove_policy
- Blob storage at `~/.dataclaw/blobs/` — full session JSON stored on disk, only metadata in SQLite

**`dataclaw/badges.py`** — Trace card signal extraction
- Outcome badges: tests_passed, tests_failed, build_failed, analysis_only, unknown
- Value badges: novel_domain, long_horizon, tool_rich, scientific_workflow, debugging
- Risk badges: secrets_detected, names_detected, private_url, manual_review
- Also: sensitivity_score (0-1), task_type, display_title, files_touched, commands_run

**`dataclaw/daemon.py`** — Background scanner + HTTP API
- Periodic scanner (60s interval) watches ~/.claude/projects/, ~/.codex/sessions/, ~/.openclaw/agents/
- HTTP server on localhost:8384 with 15 REST endpoints
- Initial scan runs in background thread so the server is responsive immediately
- Serves React SPA with fallback to index.html for client-side routing

### Phase 2: Web UI (Complete)

**`dataclaw/web/frontend/`** — React + Vite + TypeScript SPA

Views:
- **Inbox** — Trace card list with stats bar, filters (status/source/project/sort), bulk actions (shortlist/approve/block all), pagination, refresh/scan button
- **Search** — Debounced FTS search with results as trace cards
- **Session Detail** — Three-pane layout: timeline (left), transcript (center), metadata+review controls (right). Collapsible thinking blocks, expandable tool calls, [REDACTED] highlighting, reviewer notes form
- **Bundles** — Create bundles from approved sessions, view bundle details, export to disk
- **Policies** — CRUD for redaction rules (redact_string, redact_username, exclude_project, block_domain)

Components:
- TraceCard — Source icon, badges, title, stats, quick triage buttons
- BadgeChip — Colored chips for outcome/value/risk/status badges
- FilterBar — Source/project/status/sort dropdowns

Sidebar navigation with active state highlighting.

### Phase 3: Agent Skills/Plugins (Complete)

**CLI commands added:**
- `dataclaw serve [--port] [--no-browser]` — Start daemon + open browser
- `dataclaw scan [--source]` — One-shot session indexing
- `dataclaw inbox [--json] [--status] [--source] [--limit]` — Terminal/agent-parseable trace listing
- `dataclaw approve <id> [id ...] [--reason]` — Approve sessions
- `dataclaw block <id> [id ...] [--reason]` — Block sessions
- `dataclaw shortlist <id> [id ...]` — Shortlist sessions
- `dataclaw update-skill claude|openclaw|codex|cline` — Multi-agent skill install

**Safety change:** `dataclaw export` is now local-only by default. `--push` flag required to upload to HF.

**Skill files updated:**
- `docs/SKILL.md` — Workbench Mode (default) + Export Mode, teaches agents triage flow
- `AGENTS.md` — Same dual-mode structure for OpenClaw/generic agents
- `.claude/skills/dataclaw/SKILL.md` — Local copy

**Frontend dist bundled in Python package** via pyproject.toml `package-data` + MANIFEST.in.

### Tests

- 58 new tests: test_index.py, test_badges.py, test_daemon.py
- All 348 tests pass (290 existing + 58 new)

### Bug Fixes During Testing

- `msg.content` null crash in SessionDetail.tsx — fixed null-safety in RedactedText, buildTimeline, and MessageCard
- Build failure badge false positive — "BUILD FAILED" was matching test failure regex; reordered detection logic
- Daemon startup blocking — initial scan ran synchronously before HTTP server started; moved to background thread

---

### Phase 4: UX Improvements (Complete — 2026-03-18)

**Bundles.tsx — Session selection with checkboxes**
- Added `excludedIds` state to allow deselecting individual sessions from a bundle
- Scrollable session table (max 260px) with checkboxes, display title, project, source, messages, tokens
- Select-all checkbox in header; excluded rows dimmed (opacity 0.45)
- Summary shows "X of Y selected" with live source/project distribution
- Create Bundle button shows count and disables when 0 selected
- State resets on cancel and after successful create

**SessionDetail.tsx — Metadata moved to left panel**
- Left panel widened 200 → 300px
- Added between SUMMARY and PROMPT: Session Info (ID, source, model, branch, task type, started, tokens in/out, messages, tool uses, bundle), Badges, Sensitivity bar, Files Touched, Commands Run
- Right panel narrowed 280 → 240px, now contains only the Review form (status buttons, selection reason, reviewer notes, save)

---

## Current State

- **170 sessions indexed** (145 Claude Code + 25 Codex) from real local traces
- Workbench running at `http://localhost:8384`
- Code on `kaiaiagent/dataclaw` branch `ui-demo`
- Frontend dist needs to be committed (new build output after Phase 4 changes)
- **Distribution:** `pip install git+https://github.com/kaiaiagent/dataclaw.git@ui-demo` works for other engineers. PyPI publish would simplify to `pip install dataclaw` / `uvx dataclaw serve`.

---

## Not Yet Built

### Phase 5 (v1.5 — deferred)
- Span-level redaction in the UI
- Saved searches
- Duplicate clustering
- Richer outcome extraction (notebook runs, benchmark deltas)
- Desktop wrapper (Tauri/Electron)

### Ground Foundry Upload (deferred)
- GF API client (`dataclaw/gf_client.py`)
- Authenticated upload with attestation + receipt
- Server processing status tracking
- Waiting for GF backend to exist

### Product Polish
- Better display_title extraction (some show raw XML tags from Claude Code internal messages)
- Session detail: span selection for partial redaction
- Bundle builder: diversity/redundancy scoring
- Policies: server-side deny list sync
- Multi-user / lab mode

### Distribution
- PyPI publish (enables `pip install dataclaw` / `uvx dataclaw serve`)

---

## File Inventory

### New files created
```
dataclaw/index.py                          # SQLite + FTS index
dataclaw/badges.py                         # Badge computation
dataclaw/daemon.py                         # Scanner + HTTP API
dataclaw/web/frontend/                     # React SPA (full Vite project)
  src/App.tsx                              # App shell + sidebar nav
  src/api.ts                               # API client
  src/types.ts                             # TypeScript types
  src/views/Inbox.tsx                      # Inbox view
  src/views/Search.tsx                     # Search view
  src/views/SessionDetail.tsx              # Three-pane session detail
  src/views/Bundles.tsx                    # Bundle management
  src/views/Policies.tsx                   # Policy management
  src/components/TraceCard.tsx             # Trace card component
  src/components/BadgeChip.tsx             # Badge chip component
  src/components/FilterBar.tsx             # Filter bar component
  dist/                                    # Built frontend (shipped with pip)
docs/ground-foundry-scientist-workbench.md # Product spec
tests/test_index.py                        # Index tests
tests/test_badges.py                       # Badge tests
tests/test_daemon.py                       # Daemon API tests
MANIFEST.in                               # Include dist in sdist
CLAUDE.md                                 # Project instructions
```

### Modified files
```
dataclaw/cli.py          # serve, scan, inbox, approve/block/shortlist, update-skill extensions
dataclaw/config.py       # daemon_port field
pyproject.toml           # package-data for frontend dist
.gitignore               # Allow frontend dist/
AGENTS.md                # Workbench workflow for OpenClaw/agents
docs/SKILL.md            # Workbench Mode for Claude Code skill
tests/test_cli.py        # Updated test for --push default change
```
