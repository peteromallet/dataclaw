# Fork DataClaw + Add Refinery Layer

## Context

DataClaw (peteromallet/dataclaw) exports coding agent conversation traces to Hugging Face. The goal is to fork it to build a proprietary **refinery layer** for data quality scoring on top. The fork keeps the "dataclaw" name for now and maintains an upstream git relationship so privacy fixes and other improvements can be contributed back.

The exploration also identified **privacy gaps** in the workbench indexing pipeline that should be fixed — these are upstream-contributable.

**Note:** "OpenClaw" was considered as a project name but is already taken — it's an existing coding agent that DataClaw supports as a source (`~/.openclaw/agents/`). Using it would collide with existing constants, parsers, and tests.

## Phase 1: Fork Setup

1. **Clone from your fork to a new directory:**
   ```bash
   cd /Users/kaidu/llm
   git clone https://github.com/kaiaiagent/dataclaw.git dataclaw-refinery
   cd dataclaw-refinery
   git remote rename origin fork
   git remote add origin https://github.com/peteromallet/dataclaw.git
   git fetch origin
   ```
   This mirrors the remote naming from the existing checkout (`origin` = upstream, `fork` = yours).

2. **Install in dev mode:**
   ```bash
   pip install -e ".[dev]"
   ```

## Phase 2: Fix Privacy Gaps (upstream-contributable)

Branch from upstream main: `git checkout -b fix/redaction-gaps origin/main`

### Gap 1: Daemon scanner skips `redact_session()` — HIGH priority
- **File:** `dataclaw/daemon.py` lines 73-104 (`Scanner.scan_once()`)
- **Problem:** Creates Anonymizer and parses sessions, but never calls `redact_session()` before `upsert_sessions()`. Indexed sessions + blobs + FTS may contain JWT tokens, API keys, emails, etc.
- **Fix:** After `parse_project_sessions()` returns, iterate sessions and call `redact_session(session, custom_strings=config.get("redact_strings", []))` from `dataclaw.secrets`. ~5 lines of code.

### Gap 2: Custom source only redacts message content, not tool I/O
- **File:** `dataclaw/parser.py`, `_parse_custom_sessions()`
- **Problem:** Only processes `msg["content"]` with `redact_text()` + `anonymizer.text()`. Tool inputs/outputs are untouched.
- **Fix:** After building the session, call `redact_session(session)` for full recursive redaction, or extend the loop to cover `msg["thinking"]` and `msg["tool_uses"]`.

### Tests
- Add test in `tests/test_daemon.py`: mock session with JWT in tool output, verify `scan_once()` produces redacted output
- Add test in `tests/test_parser.py`: custom source session with secrets in tool_uses, verify redacted

### Contribute upstream
- Push branch to `fork`, create PR against `origin/main`

## Phase 3: Refinery Core (proprietary)

Branch from fork's main: `git checkout -b feature/refinery main`

### New directory: `dataclaw/refinery/`

```
dataclaw/refinery/
  __init__.py       # Public API: score_session(), RefineryConfig
  scorer.py         # Multi-dimensional scoring engine
  dimensions.py     # Scoring dimension definitions + weights
  filters.py        # Quality gates: min messages, dedup, privacy gate
  pipeline.py       # Orchestrator: filter -> score -> rank -> partition
```

### Scoring Dimensions (`dimensions.py`)

Each dimension scores 0.0-1.0. Built on top of existing `badges.compute_all_badges()`:

| Dimension | Source signals |
|-----------|---------------|
| `intent_clarity` | First user message length, specificity (file/function refs), error context |
| `task_complexity` | Distinct files touched, edit ops vs reads, multi-language |
| `outcome_verification` | `outcome_badge` (tests_passed/failed/build_failed) |
| `conversation_depth` | User message count, total tokens, `long_horizon` badge |
| `tool_utilization` | Tool use ratio, `tool_rich` badge |
| `code_impact` | Write/Edit tool count, files touched count |
| `error_recovery` | `debugging` badge, error->fix->verify pattern |
| `privacy_cleanliness` | Inverse of `sensitivity_score` |

### Scorer (`scorer.py`)

- Calls `compute_all_badges(session)` from `dataclaw/badges.py` (line 470)
- Adds new analysis not in badges (intent_clarity, task_complexity, code_impact specifics)
- Produces composite score (0.0-1.0) with configurable weights
- Maps to existing 1-5 scale for DB compatibility
- **Deterministic** — no LLM calls. Complements existing `dataclaw score` AI-based scoring.

### Filters (`filters.py`)

Pre-filter before scoring:
- Minimum 2 user messages
- Minimum 500 total tokens
- Near-duplicate detection (same first user message within project)
- Privacy gate: `sensitivity_score >= threshold` quarantined

### Pipeline (`pipeline.py`)

```python
def refine(sessions, config) -> list[ScoredSession]:
    # 1. Ensure redact_session() applied
    # 2. Filter trivial/duplicate sessions
    # 3. Score each session across all dimensions
    # 4. Compute composite score
    # 5. Partition into tiers: excellent/good/average/low/poor
```

### CLI Integration

Add to `dataclaw/cli.py`:
- `dataclaw refine --source claude` — run pipeline, output summary
- `dataclaw refinery-report` — show score distributions

### DB Extension

Add refinery columns to sessions table in `dataclaw/index.py`:
- `refinery_score REAL` (composite 0.0-1.0)
- `refinery_tier TEXT` (excellent/good/average/low/poor)

## Phase 4: Tests

- `tests/test_refinery.py` — unit tests for each dimension scorer
- Test: session with tests_passed + multi-file edits scores > 0.7
- Test: trivial "hello" session scores < 0.2
- Test: dedup filter catches near-duplicate sessions
- Run full suite: `pytest tests/`

## Branching Strategy

- **Upstream-compatible changes** (privacy fixes): branch from `origin/main`, PR to upstream
- **Proprietary changes** (refinery): branch from fork's main, only push to `fork`
- **Signal:** anything in `dataclaw/refinery/` is proprietary; everything else is potentially upstream-compatible
- Periodically: `git fetch origin && git merge origin/main` to sync upstream improvements

## Key Files

| File | Role |
|------|------|
| `dataclaw/badges.py` | Foundation — `compute_all_badges()` at line 470 |
| `dataclaw/daemon.py` | Privacy fix — `scan_once()` at line 73 |
| `dataclaw/parser.py` | Privacy fix — `_parse_custom_sessions()` |
| `dataclaw/secrets.py` | `redact_session()` — core redaction engine |
| `dataclaw/index.py` | Schema extension for refinery scores |
| `dataclaw/cli.py` | New CLI commands for refinery |

## Verification

1. `pytest tests/` — all existing + new tests pass
2. Privacy: `dataclaw serve`, inspect indexed session blob, confirm no raw secrets
3. Refinery: `dataclaw refine --source claude` outputs scored sessions with tier labels
4. Upstream: privacy fix branch applies cleanly to `origin/main`
