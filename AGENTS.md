# DataClaw

Review, curate, and export coding agent conversation traces from Claude Code, Codex, Gemini CLI, OpenCode, and OpenClaw.

## Install

```bash
pip install dataclaw
```

## Two Modes

### 1. Workbench Mode (default) — Review and curate traces

For scientists who want to review their AI coding traces before sharing.

```bash
dataclaw scan                              # Index sessions into local DB
dataclaw inbox --json --limit 15           # Show trace cards as JSON
dataclaw approve <session-id> --reason "good trace"  # Approve sessions
dataclaw block <session-id> --reason "proprietary"   # Block sessions
dataclaw shortlist <session-id>            # Shortlist for later review
dataclaw serve                             # Open full review UI in browser
```

**Agent workflow:**

1. Ensure installed: `pip install dataclaw`
2. Scan: `dataclaw scan`
3. Show inbox: `dataclaw inbox --json --limit 15`
4. Parse the JSON — each session has `index`, `session_id`, `display_title`, `source`, `model`, `messages`, `tokens`, `outcome_badge`, `value_badges`, `risk_badges`, `review_status`
5. Present traces to the user as a numbered list
6. Take triage instructions: "approve 1,3" or "block 2"
7. Map index numbers to session_ids and run `dataclaw approve <id>` or `dataclaw block <id>`
8. For deep review: `dataclaw serve` opens a browser with Inbox, Search, Session Detail, Bundles, and Policies views

### 2. Export Mode — Bulk export to Hugging Face

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

Key fields:
- `stage` / `stage_number` / `total_stages` — where you are
- `next_steps` — follow these in order
- `next_command` — the single most important command to run next (null if user input needed first)

### PII Audit (Stage 3)

After `dataclaw export --no-push`, follow the `next_steps` in the JSON output. The flow is:

1. **Ask the user their full name** — then grep the export for it
2. **Run the pii_commands** from the JSON output and review results with the user
3. **Ask the user what else to look for** — company names, client names, private URLs, other people's names, custom domains
4. **Deep manual scan** — sample ~20 sessions (beginning, middle, end) and look for anything sensitive the regex missed
5. **Fix and re-export** if anything found: `dataclaw config --redact "string"` then `dataclaw export --no-push`
6. **Run `dataclaw confirm` with text attestations** — pass `--full-name`, `--attest-full-name`, `--attest-sensitive`, and `--attest-manual-scan`. It runs PII scan, verifies attestations, shows project breakdown, and unlocks pushing.
7. **Push only after explicit user confirmation**: `dataclaw export --publish-attestation "User explicitly approved publishing to Hugging Face."`

## Commands Reference

```bash
# Workbench
dataclaw scan [--source claude|codex|openclaw]     # Index sessions
dataclaw inbox [--json] [--status ...] [--limit 20] # List sessions
dataclaw approve <id> [id ...] [--reason "..."]    # Approve sessions
dataclaw block <id> [id ...] [--reason "..."]      # Block sessions
dataclaw shortlist <id> [id ...]                    # Shortlist sessions
dataclaw serve [--port 8384] [--no-browser]         # Launch web UI

# Export
dataclaw status                            # Show current stage (JSON)
dataclaw prep [--source ...]               # Discover projects (JSON)
dataclaw list [--source ...]               # List all projects
dataclaw config [--repo ...] [--source ...] [--exclude ...] [--redact ...]
dataclaw export                            # Export locally (default, no upload)
dataclaw export --push --publish-attestation "..." # Upload to HF (requires confirm first)
dataclaw confirm --full-name "..." --attest-full-name "..." --attest-sensitive "..." --attest-manual-scan "..."

# Skill install
dataclaw update-skill claude               # Claude Code skill
dataclaw update-skill openclaw             # OpenClaw agents file
dataclaw update-skill codex                # Codex agents file
dataclaw update-skill cline                # Cline skill
```

## Gotchas

- **Never run bare `huggingface-cli login`** — it's interactive and will hang. Always use `--token`.
- **`--exclude`, `--redact`, `--redact-usernames` APPEND** — they never overwrite. Safe to call repeatedly.
- **Source selection is REQUIRED before export** — explicitly set `dataclaw config --source claude|codex|gemini|opencode|openclaw|all` (or pass `--source ...` on export).
- **`dataclaw prep` outputs pure JSON** — parse it directly.
- **Always export with `--no-push` first** — review before publishing.
- **`dataclaw export` (push) requires `dataclaw confirm` first** — it will refuse otherwise. Re-exporting with `--no-push` resets this.
- **PII audit is critical** — automated redaction is not foolproof.
- **Large exports take time** — 500+ sessions may take 1-3 minutes. Use a generous timeout.
