# DataClaw

Export Claude Code, Codex, Gemini CLI, and OpenCode conversation history to Hugging Face.

## THE RULE

**Every `dataclaw` command outputs `next_steps`. FOLLOW THEM.**

Do not memorize the flow. Do not skip steps. Do not improvise.
Run the command → read the output → follow `next_steps`. That's it.

The CLI tracks your stage (1-4: auth → configure → review → done).
`dataclaw export` (push) is **gated** — you must run `dataclaw confirm` first or it will refuse.

## Getting Started

Run `dataclaw status` (or `dataclaw prep` for full details) and follow the `next_steps`.

## Output Format

- `dataclaw prep`, `dataclaw config`, `dataclaw status`, and `dataclaw confirm` output pure JSON
- `dataclaw export` outputs human-readable text followed by `---DATACLAW_JSON---` and a JSON block
- Always parse the JSON and act on `next_steps`

Key fields:
- `stage` / `stage_number` / `total_stages` — where you are
- `next_steps` — follow these in order
- `next_command` — the single most important command to run next (null if user input needed first)

## PII Audit (Stage 3)

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
dataclaw status                            # Show current stage and next steps (JSON)
dataclaw prep                              # Discover projects, check HF auth (JSON)
dataclaw prep --source all                 # All sources (Claude + Codex + Gemini + OpenCode)
dataclaw prep --source claude              # Only Claude Code sessions
dataclaw prep --source codex               # Only Codex sessions
dataclaw prep --source gemini              # Only Gemini CLI sessions
dataclaw prep --source opencode            # Only OpenCode sessions
dataclaw confirm --full-name "NAME" --attest-full-name "..." --attest-sensitive "..." --attest-manual-scan "..." # Scan PII, verify attestations, unlock pushing (JSON)
dataclaw confirm --file /path/to/file.jsonl --full-name "NAME" --attest-full-name "..." --attest-sensitive "..." --attest-manual-scan "..." # Confirm a specific export file
dataclaw list                              # List all projects with exclusion status
dataclaw list --source all                 # List all sources
dataclaw list --source codex               # List only Codex projects
dataclaw config                            # Show current config
dataclaw config --repo user/my-dataset     # Set HF repo
dataclaw config --source all               # REQUIRED source scope: claude|codex|gemini|opencode|all
dataclaw config --exclude "a,b"            # Add excluded projects (appends)
dataclaw config --redact "str1,str2"       # Add strings to redact (appends)
dataclaw config --redact-usernames "u1,u2" # Add usernames to anonymize (appends)
dataclaw config --confirm-projects         # Mark project selection as confirmed
dataclaw export --publish-attestation "..." # Export and push (requires dataclaw confirm first)
dataclaw export --no-push                  # Export locally only
dataclaw export --source all --no-push     # Export all sources locally
dataclaw export --source codex --no-push   # Export only Codex sessions
dataclaw export --source claude --no-push  # Export only Claude Code sessions
dataclaw export --source gemini --no-push  # Export only Gemini CLI sessions
dataclaw export --source opencode --no-push # Export only OpenCode sessions
dataclaw export --all-projects             # Include everything (ignore exclusions)
dataclaw export --no-thinking              # Exclude extended thinking blocks
dataclaw export -o /path/to/file.jsonl     # Custom output path
```

## Gotchas

- **Never run bare `huggingface-cli login`** — it's interactive and will hang. Always use `--token`.
- **`--exclude`, `--redact`, `--redact-usernames` APPEND** — they never overwrite. Safe to call repeatedly.
- **Source selection is REQUIRED before export** — explicitly set `dataclaw config --source claude|codex|gemini|opencode|all` (or pass `--source ...` on export).
- **`dataclaw prep` outputs pure JSON** — parse it directly.
- **Always export with `--no-push` first** — review before publishing.
- **`dataclaw export` (push) requires `dataclaw confirm` first** — it will refuse otherwise. Re-exporting with `--no-push` resets this.
- **PII audit is critical** — automated redaction is not foolproof.
- **Large exports take time** — 500+ sessions may take 1-3 minutes. Use a generous timeout.

## Install

```bash
pip install dataclaw
```
