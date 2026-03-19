# Scoring Traces: Design Doc

## Privacy Guarantee

**Everything runs locally on the user's machine.** No data leaves the user's computer at any point during scanning, scoring, or reviewing. The only moment data touches the internet is when the user explicitly creates a bundle and pushes it to Hugging Face. Even then, only the sessions the user hand-picked are included.

```
LOCAL (your machine only):
  ✓ Scanning sessions from Claude Code / Codex logs
  ✓ AI scoring (Claude Code evaluates in your conversation)
  ✓ Reviewing in the browser workbench
  ✓ Adding comments, adjusting scores, approving/blocking
  ✓ Creating bundles
  ✓ Exporting to local disk

SHARED (only when you explicitly choose):
  ✗ Pushing a bundle to Hugging Face (requires your confirmation)
```

---

## User Journey

One prompt in Claude Code. Browser opens immediately so the user can watch.

```
User: "I want to review and share my coding traces"
      (or: /dataclaw)

Claude:
  1. pip install dataclaw (if needed)
  2. dataclaw scan                          → "Found 47 sessions across 5 projects"
  3. dataclaw serve                         → opens browser immediately
     "Your workbench is open at localhost:8384.
      Everything here is local — nothing is shared until you explicitly export.
      I'll now score your sessions. You can watch in the browser (refresh to see updates)."

  4. dataclaw score-view --batch ...        → Claude reads 5 sessions at a time
     dataclaw set-score ... (×47)           → scores each, ~10 batches, ~3 min
     (user can refresh browser to watch scores appear)

  5. "Scored 47 sessions:
      12 excellent (5), 18 good (4), 10 average (3), 5 low (2), 2 poor (1)
      Auto-approve the 30 rated 4-5 and block the 7 rated 1-2?
      The 10 average ones are left for your review in the browser."

  6. User: "yes"

  7. dataclaw approve ... (×30)
     dataclaw block ... (×7)
     "Done. Refresh your browser to see the updates.
      Review the 10 borderline sessions, add any comments, then go to
      Exports to create a bundle. Nothing is shared until you say so."

User in browser (5 minutes):
  - Sessions sorted by AI score, 30 already approved, 7 blocked
  - AI score + reason visible on every card — user can see WHY
  - Click any session → full transcript + easy feedback panel on the right
  - Reviews the 10 borderline sessions, adds comments
  - Goes to Exports → sees exactly what's in the bundle
  - Exports to disk or pushes to HF (only this step shares data)
```

**Total user effort**: 1 prompt + 1 confirmation + ~5 min browser review.

---

## What We Build

### 1. CLI: `dataclaw score-view`

Condensed session output optimized for AI evaluation.

**Single session**: `dataclaw score-view <session_id>`
```
Session: abc123
Source: claude | Model: sonnet | Project: myapp
Duration: 12m | Tokens: 15.2k in / 8.3k out | Messages: 5 user / 4 asst
Task type: debugging | Outcome: tests_passed
Value: debugging, tool_rich | Risk: (none) | Sensitivity: 0.05

--- FIRST USER MESSAGE ---
I'm getting a TypeError when I try to call the login function...

--- CONVERSATION FLOW ---
#0 [User] Reports TypeError in login function
#1 [Asst] Reads auth.py, identifies missing null check
   → Read(auth.py) ok
   → Edit(auth.py) ok
#2 [User] Thanks, but now tests fail
#3 [Asst] Updates test assertion, runs pytest
   → Edit(test_auth.py) ok
   → Bash(pytest tests/test_auth.py) ok — "3 passed"
#4 [User] All working now

--- FILES TOUCHED ---
auth.py, test_auth.py

--- COMMANDS RUN ---
pytest tests/test_auth.py
```

**Batch mode** (5 sessions at once for efficiency): `dataclaw score-view --batch --limit 5 --offset 0`
```
=== SESSION 1/5: abc123 ===
Source: claude | Model: sonnet | Project: myapp | 12m | 15k tokens
Task: debugging | Outcome: tests_passed | 5 user + 4 asst msgs
First msg: "I'm getting a TypeError when I try to call the login function..."
Flow: User→bug report, Asst→Read+Edit(auth.py), User→tests fail, Asst→Edit(test)+Bash(pytest)→3 passed, User→confirmed
Files: auth.py, test_auth.py

=== SESSION 2/5: def456 ===
Source: codex | Model: gpt-4 | Project: webapp | 1m | 800 tokens
Task: unknown | Outcome: unknown | 1 user + 1 asst msgs
First msg: "hello"
Flow: User→greeting, Asst→greeting response
Files: (none)

...
```

Batch mode is ~6-8 lines per session. Claude reads all 5, scores all 5 in one turn. 50 sessions = 10 batches = ~3 minutes.

### 2. CLI: `dataclaw set-score`

`dataclaw set-score <session_id> --quality <1-5> --reason "..."`

Stores the AI quality score. Output:
```json
{"session_id": "abc123", "ai_quality_score": 4, "ok": true}
```

Accepts multiple IDs: `dataclaw set-score id1 id2 id3 --quality 5 --reason "..."`

### 3. CLI: `dataclaw score-batch`

`dataclaw score-batch [--limit 50] [--source claude|codex]`

Lists unscored sessions (JSON for agent parsing):
```json
[
  {"session_id": "abc123", "display_title": "Fix auth bug", "task_type": "debugging", "outcome_badge": "tests_passed"},
  ...
]
```

### 4. DB Schema

**File**: `dataclaw/index.py`

Add to `sessions` table:
```sql
ai_quality_score   INTEGER,   -- 1-5 from AI scoring
ai_score_reason    TEXT        -- AI's one-line explanation
```

Migration: ALTER TABLE ADD COLUMN, ignore if exists.
Update `update_session()` to accept the new fields.

### 5. Skill: Update `/dataclaw`

**File**: `.claude/skills/dataclaw/SKILL.md`

Enhance **Workbench Mode** to include scoring as the default flow. Key change: **open the browser first**, then score, so the user can watch.

```markdown
### Full Workflow (recommended)

When the user wants to review and curate traces:

1. Scan and open:
   ```bash
   dataclaw scan
   dataclaw serve
   ```
   Tell the user: "Your workbench is open at localhost:8384. Everything here
   is 100% local — nothing is shared until you explicitly export a bundle.
   I'll now score your sessions. Refresh the browser to see updates."

2. Get unscored sessions and score in batches of 5:
   ```bash
   dataclaw score-batch --limit 100
   dataclaw score-view --batch --limit 5 --offset N
   ```
   Read the output. Score each session 1-5 using this rubric:

   **5 = Excellent** — Clear non-trivial task. Verified outcome (tests pass,
   build succeeds). Rich tool usage, multi-step problem-solving.
   **4 = Good** — Clear task, useful outcome. Some tool usage and verification.
   **3 = Average** — Routine task. Partial/unverified outcome. Basic interaction.
   **2 = Low** — Vague/trivial task. Failed or unclear outcome.
   **1 = Poor** — No real coding task. Trivially short or broken.

   Evaluate: intent clarity, outcome success, conversation substance, agent quality.

   For Claude Code: value IDE workflows (read→edit→test), debugging, multi-file changes.
   For Codex: value clear specs, multi-step implementations.

   Store each score:
   ```bash
   dataclaw set-score <id> --quality <N> --reason "<1-2 sentence explanation>"
   ```

3. Summarize and triage:
   "Scored N sessions. X excellent, Y good, Z average, W low."
   Ask: "Auto-approve the sessions rated 4-5 and block the 1-2s?
   The average ones are left for your review in the browser."
   If confirmed:
   - Score 4-5 → `dataclaw approve <id> --reason "<ai reason>"`
   - Score 1-2 → `dataclaw block <id> --reason "<ai reason>"`
   - Score 3 → leave for manual review

4. Hand off to browser:
   "Done. Refresh your browser to see all scores and triage results.
   You can:
   - Click any session to read the full transcript
   - Add your own comments and adjust scores in the review panel
   - Go to Exports to bundle the approved sessions
   Nothing leaves your machine until you choose to export."
```

### 6. Skill: `/dataclaw-score` (optional, for re-scoring)

**File**: `.claude/skills/dataclaw-score/SKILL.md`

For scoring individual sessions or re-scoring. Uses `!`dataclaw score-view $ARGUMENTS`` dynamic injection. Same rubric as above. Simpler, single-session focused.

### 7. Frontend: Display scores + user feedback UX

The browser is the user's window into what's happening locally and what will be shared. Two priorities: (a) show AI scores prominently, (b) make user feedback dead-simple.

#### Types: `types.ts`
```ts
ai_quality_score: number | null;
ai_score_reason: string | null;
```

#### TraceCard: score + quick feedback

Show AI score as a prominent colored badge on every card:
- `5` green / `4` light-green / `3` yellow / `2` orange / `1` red / unscored: gray `?`

Add an **inline comment button** on each card (speech bubble icon) that expands a small text input right on the card — user can add a quick note without opening the full session detail. Clicking the icon toggles a small form:
```
┌──────────────────────────────────────────────────────────┐
│  ●4  Fix auth bug in login module         2h ago    [💬] │
│  myapp | sonnet | 5 msgs | 15k tokens                   │
│  [Approved] [tests_passed] [debugging] [tool_rich]       │
│                                                          │
│  ┌─ Your comment ──────────────────────────────────────┐ │
│  │ Good trace but the initial prompt could be clearer  │ │
│  └──────────────────────────────── [Save] [Cancel] ────┘ │
└──────────────────────────────────────────────────────────┘
```

The comment saves to `reviewer_notes` via the existing `POST /api/sessions/<id>` endpoint.

#### SessionDetail: right panel = user feedback (always visible)

The right panel is the user's feedback station. It should feel like a simple form, not buried controls. Layout:

```
┌─ YOUR REVIEW ──────────────────────┐
│                                    │
│  Status:                           │
│  [New] [Shortlist] [Approve] [Block]│
│                                    │
│  AI Score: ●4 Good                 │
│  "Clear debugging task with        │
│   verified fix via pytest"         │
│                                    │
│  Your Rating (override AI):        │
│  [1] [2] [3] [4] [5]              │
│                                    │
│  Why selected / Why not:           │
│  ┌────────────────────────────┐    │
│  │                            │    │
│  └────────────────────────────┘    │
│                                    │
│  Your Notes:                       │
│  ┌────────────────────────────┐    │
│  │ Good trace but the initial │    │
│  │ prompt could be clearer    │    │
│  └────────────────────────────┘    │
│                                    │
│  [Save Review]                     │
│                                    │
│  ── Privacy ──────────────────     │
│  This review is stored locally.    │
│  Only approved sessions can be     │
│  bundled and shared.               │
└────────────────────────────────────┘
```

Key changes from current right panel:
- Show the AI score + reason prominently at the top (so user knows the AI's assessment before deciding)
- Add explicit **user rating buttons (1-5)** to override AI score — saves to `ai_quality_score` as an override
- Keep Selection Reason + Reviewer Notes (existing fields)
- Add a **privacy footer** reminding the user that everything is local

#### Inbox: recommendation banner + privacy reminder

When scored sessions exist, show a banner:
```
┌─────────────────────────────────────────────────────────────────┐
│  🔒 Everything here is local. Only bundled exports leave your   │
│  machine.                                                       │
│                                                                 │
│  AI scored 30 sessions as high-quality (4-5).                   │
│  [Approve All Recommended]    [Review First →]                  │
└─────────────────────────────────────────────────────────────────┘
```

#### FilterBar: score filter + sort

Add score filter dropdown:
- All scores / Excellent (5) / Good (4) / Average (3) / Low (1-2) / Unscored

Add sort option: "Highest quality" (ai_quality_score desc).

#### Bundles (Exports page): show what's being shared

When creating a bundle, show a clear summary of what will leave the machine:
```
┌─ Creating Bundle ──────────────────────────────────────────┐
│                                                            │
│  📦 This bundle contains 30 sessions.                      │
│                                                            │
│  What's included:                                          │
│  - Anonymized conversation transcripts                     │
│  - Session metadata (tokens, duration, model)              │
│  - Redacted content (secrets, PII removed)                 │
│                                                            │
│  What's NOT included:                                      │
│  - Your file contents or source code                       │
│  - Your reviewer notes or comments                         │
│  - Sessions you didn't approve                             │
│                                                            │
│  Exporting to disk keeps everything local.                 │
│  Pushing to HF makes the bundle publicly available.        │
│                                                            │
│  [Export to Disk]   [Push to Hugging Face]                 │
└────────────────────────────────────────────────────────────┘
```

---

## Files to Modify/Create

| File | Change |
|------|--------|
| `dataclaw/cli.py` | `score-view`, `set-score`, `score-batch` commands |
| `dataclaw/index.py` | Schema columns, migration, `update_session()`, unscored query |
| `.claude/skills/dataclaw/SKILL.md` | Add scoring workflow + privacy messaging |
| `.claude/skills/dataclaw-score/SKILL.md` | **New** — single-session scoring skill |
| `dataclaw/web/frontend/src/types.ts` | `ai_quality_score`, `ai_score_reason` |
| `dataclaw/web/frontend/src/components/TraceCard.tsx` | Score badge + inline comment button |
| `dataclaw/web/frontend/src/components/FilterBar.tsx` | Score filter + sort option |
| `dataclaw/web/frontend/src/views/Inbox.tsx` | Recommendation banner + privacy reminder |
| `dataclaw/web/frontend/src/views/SessionDetail.tsx` | Enhanced review panel: AI score, user rating override, privacy footer |
| `dataclaw/web/frontend/src/views/Bundles.tsx` | "What's included" summary on bundle creation |

---

## Verification

1. `pytest tests/` — existing tests pass
2. `dataclaw scan && dataclaw score-batch` — lists unscored sessions
3. `dataclaw score-view <id>` — condensed single view
4. `dataclaw score-view --batch --limit 5` — compact batch view
5. `dataclaw set-score <id> --quality 4 --reason "test"` — stores score
6. `/dataclaw` in Claude Code — full workflow: scan → serve → score → triage
7. `npx tsc --noEmit && npx vite build` — frontend builds
8. Browser: scores visible in TraceCard, inline comment works, recommendation banner shows
9. Browser: SessionDetail right panel shows AI score + user rating override + privacy note
10. Browser: Bundles page shows "what's included" summary

---

## Future (not in v1)

- **`claude -p` batch mode**: `dataclaw score --headless --batch` for unattended scoring (100+ sessions)
- **Score calibration**: compare AI scores to user overrides, adjust rubric
- **Per-project scoring**: different rubrics for different project types
- **Export score metadata**: include AI scores in the JSONL export
- **Real-time updates**: WebSocket push so browser auto-refreshes as scores come in
