---
name: dataclaw
description: >
  Review, curate, and export coding agent conversation traces. Use when the user asks to
  review their traces, curate sessions, manage their workbench, export conversations,
  configure DataClaw, or review PII/secrets in exports.
allowed-tools: Bash(dataclaw *), Bash(pip install dataclaw*), Bash(grep *)
---

<!-- dataclaw-begin -->

# DataClaw Skill

DataClaw helps scientists review, curate, and export their coding agent traces from Claude Code, Codex, and OpenClaw.

## Prerequisite

```bash
command -v dataclaw >/dev/null 2>&1 && echo "dataclaw: installed" || pip install dataclaw
```

Always ensure dataclaw is installed before running any command.

---

When the user asks to review traces, curate sessions, look at their work history, or manage data:

### Quick Start

```bash
dataclaw scan                              # Index sessions into local DB
dataclaw inbox --json --limit 15           # Show trace cards (JSON for you to parse)
```

Parse the JSON output. Each session has: `index`, `session_id`, `display_title`, `source`, `model`, `messages`, `tokens`, `outcome_badge`, `value_badges`, `risk_badges`, `review_status`.

Present the traces to the user as a numbered list showing title, source, badges, and status. Then ask: **"Quick triage here, or open the full review UI?"**

### Quick Triage (in-conversation)

Show the user their traces and take instructions like "approve 1,3,5" or "block 2":

```bash
dataclaw approve <session-id> [session-id ...] --reason "good debugging trace"
dataclaw block <session-id> [session-id ...] --reason "contains proprietary code"
dataclaw shortlist <session-id> [session-id ...]
```

Map the user's index numbers to session_ids from the inbox JSON output.

After triage, ask: **"Create a bundle from the approved sessions?"**

### Full Review (web UI)

When the user wants deeper review — reading full transcripts, searching across sessions, or assembling bundles:

```bash
dataclaw serve
```

This opens a browser at `localhost:8384` with:
- **Inbox**: Trace cards with value/risk/outcome badges and one-click triage
- **Search**: Full-text search across all session transcripts
- **Session Detail**: Three-pane view (timeline | transcript | metadata)
- **Bundles**: Assemble and export curated upload sets
- **Policies**: Manage redaction rules and project exclusions

Tell the user: "The workbench is open in your browser. Use the Inbox to triage traces, Search to find specific sessions, and Bundles to assemble what you want to share."

### AI-Powered Scoring (recommended first step)

When the user wants to review traces, score them first for quality:

1. Scan and open the workbench:
   ```bash
   dataclaw scan
   dataclaw serve
   ```
   Tell the user: "Your workbench is open at localhost:8384. Everything here
   is 100% local — nothing is shared until you explicitly export a bundle.
   I'll now score your sessions. Refresh the browser to see updates."

2. Auto-score with one command:
   ```bash
   dataclaw score --batch --auto-triage --limit 20
   ```
   This automatically scores all unscored sessions via `claude -p` AND triages them:
   - Score 4-5 → approved
   - Score 1-2 → blocked
   - Score 3 → left for manual review

   Parse the JSON summary and report: "Scored N sessions. X excellent, Y good, Z average, W low. Approved A, blocked B, C left for your review."

   To score without auto-triage, omit the flag:
   ```bash
   dataclaw score --batch --limit 20
   ```
   Then ask: "Auto-approve the sessions rated 4-5 and block the 1-2s?"
   If confirmed, run approve/block commands for the relevant session IDs from the results.

3. Hand off to browser:
   "Done. Refresh your browser to see all scores and triage results. You can click any session to read the full transcript, adjust scores, and go to Bundles to assemble what you want to share. Nothing leaves your machine until you choose to export."

**For hands-on review of specific sessions**, use `dataclaw score-view <id>` to read the session, then `dataclaw set-score <id> --quality N --reason '...'` to record your score manually.

### Workbench Commands

```bash
dataclaw scan [--source claude|codex|openclaw]     # Index sessions into local DB
dataclaw inbox [--status new|shortlisted|approved|blocked] [--limit 20]  # Terminal view
dataclaw inbox --json [--status ...] [--limit 20]  # JSON output for agent parsing
dataclaw approve <id> [id ...] [--reason "..."]    # Approve sessions
dataclaw block <id> [id ...] [--reason "..."]      # Block sessions
dataclaw shortlist <id> [id ...]                    # Shortlist sessions
dataclaw serve [--port 8384] [--no-browser]         # Launch web UI
dataclaw score --batch --auto-triage --limit 20  # Score + auto-approve/block (recommended)
dataclaw score --batch --limit 10        # Batch auto-score without triage
dataclaw score <session-id>              # Auto-score a single session
dataclaw score --dry-run <session-id>    # Preview without calling claude
dataclaw score-view <session-id>          # AI-friendly condensed session view
dataclaw score-view --batch --limit 5     # Batch view for manual scoring
dataclaw set-score <id> --quality N --reason "..."  # Record manual quality score
dataclaw score-batch [--limit 50]         # List unscored sessions (JSON)
```

## Gotchas

- **`--exclude`, `--redact`, `--redact-usernames` APPEND** — they never overwrite.
- **`dataclaw inbox --json`** is the preferred way for agents to read trace data.
- **`dataclaw approve/block/shortlist`** output JSON with results.
- **`dataclaw serve`** opens a browser automatically. Use `--no-browser` to suppress.

<!-- dataclaw-end -->
