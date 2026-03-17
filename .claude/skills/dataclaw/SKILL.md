---
name: dataclaw
description: >
  Review, curate, and export coding agent conversation traces. Use when the user asks to
  review their traces, curate sessions, manage their workbench, export conversations,
  upload to Hugging Face, configure DataClaw, or review PII/secrets in exports.
allowed-tools: Bash(dataclaw *), Bash(huggingface-cli login *), Bash(pip install dataclaw*), Bash(grep *)
---

<!-- dataclaw-begin -->

# DataClaw Skill

DataClaw helps scientists review, curate, and share their coding agent traces from Claude Code, Codex, and OpenClaw.

## Prerequisite

```bash
command -v dataclaw >/dev/null 2>&1 && echo "dataclaw: installed" || pip install dataclaw
```

Always ensure dataclaw is installed before running any command.

## Two Modes

DataClaw has two workflows:

1. **Workbench Mode** — Review and curate traces (local-first, scientist-facing)
2. **Export Mode** — Bulk export to Hugging Face (the original flow)

Default to **Workbench Mode** unless the user specifically asks about Hugging Face or bulk export.

---

## Workbench Mode

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

### Workbench Commands

```bash
dataclaw scan [--source claude|codex|openclaw]     # Index sessions into local DB
dataclaw inbox [--status new|shortlisted|approved|blocked] [--limit 20]  # Terminal view
dataclaw inbox --json [--status ...] [--limit 20]  # JSON output for agent parsing
dataclaw approve <id> [id ...] [--reason "..."]    # Approve sessions
dataclaw block <id> [id ...] [--reason "..."]      # Block sessions
dataclaw shortlist <id> [id ...]                    # Shortlist sessions
dataclaw serve [--port 8384] [--no-browser]         # Launch web UI
```

---

## Export Mode (Hugging Face)

When the user asks to export to Hugging Face, upload their dataset, or push conversations:

### THE RULE

**Every `dataclaw` command outputs `next_steps`. FOLLOW THEM.**

Do not memorize the flow. Do not skip steps. Do not improvise.
Run the command → read the output → follow `next_steps`. That's it.

The CLI tracks your stage (1-4: auth → configure → review → done).
`dataclaw export` (push) is **gated** — you must run `dataclaw confirm` first or it will refuse.

### Getting Started

Run `dataclaw status` (or `dataclaw prep` for full details) and follow the `next_steps`.

### Output Format

- `dataclaw prep`, `dataclaw config`, `dataclaw status`, and `dataclaw confirm` output pure JSON
- `dataclaw export` outputs human-readable text followed by `---DATACLAW_JSON---` and a JSON block
- Always parse the JSON and act on `next_steps`

### PII Audit (Stage 3)

After `dataclaw export --no-push`, follow the `next_steps` in the JSON output. The flow is:

1. **Ask the user their full name** — then grep the export for it
2. **Run the pii_commands** from the JSON output and review results with the user
3. **Ask the user what else to look for** — company names, client names, private URLs, other people's names, custom domains
4. **Deep manual scan** — sample ~20 sessions (beginning, middle, end) and look for anything sensitive the regex missed
5. **Fix and re-export** if anything found: `dataclaw config --redact "string"` then `dataclaw export --no-push`
6. **Run `dataclaw confirm` with text attestations** — pass `--full-name`, `--attest-full-name`, `--attest-sensitive`, and `--attest-manual-scan`. It runs PII scan, verifies attestations, shows project breakdown, and unlocks pushing.
7. **Push only after explicit user confirmation**: `dataclaw export --publish-attestation "User explicitly approved publishing to Hugging Face."`

### Export Commands

```bash
dataclaw status                            # Show current stage and next steps (JSON)
dataclaw prep [--source all|claude|codex|gemini|opencode|openclaw]  # Discover projects (JSON)
dataclaw list [--source ...]               # List all projects with exclusion status
dataclaw config --source all               # REQUIRED source scope
dataclaw config --exclude "a,b"            # Add excluded projects (appends)
dataclaw config --redact "str1,str2"       # Add strings to redact (appends)
dataclaw config --redact-usernames "u1,u2" # Add usernames to anonymize (appends)
dataclaw config --confirm-projects         # Mark project selection as confirmed
dataclaw config --repo user/my-dataset     # Set HF repo
dataclaw export                            # Export locally (default, no upload)
dataclaw export --push --publish-attestation "..." # Upload to HF (requires confirm first)
dataclaw confirm --full-name "NAME" --attest-full-name "..." --attest-sensitive "..." --attest-manual-scan "..."
```

---

## Install Skill for Other Agents

```bash
dataclaw update-skill claude               # Install to .claude/skills/dataclaw/
dataclaw update-skill openclaw             # Install to project root as DATACLAW_AGENTS.md
dataclaw update-skill codex                # Install to project root as DATACLAW_AGENTS.md
dataclaw update-skill cline                # Install to .cline/dataclaw/
```

## Gotchas

- **Never run bare `huggingface-cli login`** — it's interactive and will hang. Always use `--token`.
- **`--exclude`, `--redact`, `--redact-usernames` APPEND** — they never overwrite.
- **`dataclaw inbox --json`** is the preferred way for agents to read trace data.
- **`dataclaw approve/block/shortlist`** output JSON with results.
- **`dataclaw serve`** opens a browser automatically. Use `--no-browser` to suppress.

<!-- dataclaw-end -->
