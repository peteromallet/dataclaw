# DataClaw

> **This is a performance art project.** Anthropic built their models on the world's freely shared information, then introduced increasingly [dystopian data policies](https://www.anthropic.com/news/detecting-and-preventing-distillation-attacks) to stop anyone else from doing the same with their data - pulling up the ladder behind them. DataClaw lets you throw the ladder back down. The dataset it produces is yours to share.

Turn your Claude Code, Codex, and other coding-agent conversation history into structured data and publish it to Hugging Face with a single command. DataClaw parses session logs, redacts secrets and PII, and uploads the result as a ready-to-use dataset.

![DataClaw](dataclaw.jpeg)

Every export is tagged **`dataclaw`** on Hugging Face. Together, they may someday form a growing [distributed dataset](https://huggingface.co/datasets?other=dataclaw) of real-world human-AI coding collaboration.

## Give this to your agent

Paste this into Claude Code, Codex, or any coding agent:

```
Help me export my Claude Code, Codex, and other coding-agent conversation history to Hugging Face using DataClaw.
Install it, then walk me through the process.

STEP 1 - INSTALL
  pip install -U dataclaw
  If that fails: git clone https://github.com/banodoco/dataclaw.git /tmp/dataclaw && pip install -U /tmp/dataclaw
  If that also fails, ask the user where the source is.

STEP 2 - INSTALL SKILL
  Skill support is currently only available for Claude Code.
  dataclaw update-skill claude
  For other agentic tools, skip this step and do not improvise a custom flow - follow the instructions in DataClaw's output on each step, especially next_steps and next_command.

STEP 3 - PREP
  dataclaw prep
  Every dataclaw command outputs next_steps in its JSON - follow them through the entire flow.

STEP 3A - CHOOSE SOURCE SCOPE (REQUIRED BEFORE EXPORT)
  Ask the user explicitly which source scope to export: a supported source key such as claude or codex, or all.
  dataclaw config --source all
  Do not export until source scope is explicitly confirmed.

STEP 3B - CHOOSE PROJECT SCOPE (REQUIRED BEFORE EXPORT)
  dataclaw list --source all
  Send the FULL project/folder list to the user in a message (name, source, sessions, size, excluded).
  Ask which projects to exclude.
  dataclaw config --exclude "project1,project2" OR dataclaw config --confirm-projects
  Do not export until folder selection is explicitly confirmed.

STEP 3C - SET REDACTED STRINGS
  Ask the user what additional strings should always be redacted, such as company names, client names, domains, internal URLs, or secrets that regex might miss.
  dataclaw config --redact "string1,string2"
  dataclaw config --redact-usernames "user1,user2"
  Only add these after explicit user confirmation.

STEP 4 - EXPORT LOCALLY
  dataclaw export --no-push --output dataclaw_export.jsonl

STEP 5 - REVIEW AND CONFIRM (REQUIRED BEFORE PUSH)
  Review PII findings and apply excludes/redactions as needed.
  Full name is requested for an exact-name privacy scan against the export.
  If the user declines sharing full name, use --skip-full-name-scan and attest the skip reason.
  dataclaw confirm --full-name "THEIR FULL NAME" --attest-full-name "..." --attest-sensitive "..." --attest-manual-scan "..."

STEP 6 - PUBLISH (ONLY AFTER EXPLICIT USER APPROVAL)
  dataclaw export --publish-attestation "User explicitly approved publishing to Hugging Face."
  Never publish unless the user explicitly says yes.

IF ANY COMMAND FAILS DUE TO A SKIPPED STEP:
  Restate the 6-step checklist above and resume from the blocked step (do not skip ahead).

IMPORTANT: Never run bare `hf auth login` when automating this with an agent - always use `--token`.
IMPORTANT: Always export with --no-push first and review for PII before publishing.
```

## Manual usage (without an agent)

```bash
# STEP 1 - INSTALL
pip install -U dataclaw
hf auth login --token YOUR_TOKEN

# STEP 3 - PREP
dataclaw prep
dataclaw config --repo username/my-personal-codex-data

# STEP 3A - CHOOSE SOURCE SCOPE
dataclaw config --source all  # REQUIRED: choose a supported source key or all

# STEP 3B - CHOOSE PROJECT SCOPE
dataclaw list --source all  # Present full list and confirm folder scope before export
dataclaw config --exclude "personal-stuff,scratch"  # or: dataclaw config --confirm-projects

# STEP 3C - SET REDACTED STRINGS
dataclaw config --redact-usernames "my_github_handle,my_discord_name"
dataclaw config --redact "my-domain.com,my-secret-project"

# STEP 4 - EXPORT LOCALLY
dataclaw export --no-push

# STEP 5 - REVIEW AND CONFIRM
dataclaw confirm \
  --full-name "YOUR FULL NAME" \
  --attest-full-name "Asked for full name and scanned export for YOUR FULL NAME." \
  --attest-sensitive "Asked about company/client/internal names and private URLs; none found or redactions updated." \
  --attest-manual-scan "Manually scanned 20 sessions across beginning/middle/end and reviewed findings."

# Or: if user declines sharing full name
dataclaw confirm \
  --skip-full-name-scan \
  --attest-full-name "User declined to share full name; skipped exact-name scan." \
  --attest-sensitive "Asked about company/client/internal names and private URLs; none found or redactions updated." \
  --attest-manual-scan "Manually scanned 20 sessions across beginning/middle/end and reviewed findings."

# STEP 6 - PUBLISH
dataclaw export --publish-attestation "User explicitly approved publishing to Hugging Face."
```

Step 2 (INSTALL SKILL) is omitted in manual usage.

### Commands

| Command | Description |
|---------|-------------|
| `dataclaw status` | Show current stage and next steps |
| `dataclaw prep` | Discover projects, check HF auth, output JSON |
| `dataclaw prep --source <source\|all>` | Prep with an explicit source scope |
| `dataclaw list` | List all projects with exclusion status |
| `dataclaw list --source <source\|all>` | List projects for a specific source scope |
| `dataclaw config` | Show current config |
| `dataclaw config --repo user/my-personal-codex-data` | Set HF repo |
| `dataclaw config --source <source\|all>` | REQUIRED source scope selection (examples include `claude`, `codex`, and others) |
| `dataclaw config --exclude "a,b"` | Add excluded projects (appends) |
| `dataclaw config --redact "str1,str2"` | Add strings to always redact (appends) |
| `dataclaw config --redact-usernames "u1,u2"` | Add usernames to anonymize (appends) |
| `dataclaw config --confirm-projects` | Mark project selection as confirmed |
| `dataclaw export --no-push` | Export locally only (always do this first) |
| `dataclaw export --source <source\|all> --no-push` | Export a chosen source scope locally |
| `dataclaw confirm --full-name "NAME" --attest-full-name "..." --attest-sensitive "..." --attest-manual-scan "..."` | Scan for PII, run exact-name privacy check, verify review attestations, unlock pushing |
| `dataclaw confirm --skip-full-name-scan --attest-full-name "..." --attest-sensitive "..." --attest-manual-scan "..."` | Skip exact-name scan when user declines sharing full name (requires skip attestation) |
| `dataclaw export --publish-attestation "..."` | Export and push (requires `dataclaw confirm` first) |
| `dataclaw export --all-projects` | Include everything (ignore exclusions) |
| `dataclaw export --no-thinking` | Exclude extended thinking blocks |
| `dataclaw jsonl-to-yaml [input.jsonl]` | Convert an export JSONL file to human-readable YAML |
| `dataclaw diff-jsonl --old old.jsonl --new new.jsonl` | Structurally diff two export JSONL files and write YAML |
| `dataclaw update-skill claude` | Install/update the dataclaw skill for Claude Code |

## What gets exported

- User messages - Including voice transcripts and images
- Assistant responses
- Assistant thinking - Opt out with `--no-thinking`
- Tool calls - Tool name, inputs, outputs
- Token usage - Input/output tokens per session
- Metadata - Model name, git branch, timestamps

### Privacy & Redaction

DataClaw applies multiple layers of protection:

1. Username redaction - Your OS username + any configured usernames replaced with stable hashes
2. Secret redaction - Regex patterns catch JWT tokens, API keys (Anthropic, OpenAI, HF, GitHub, AWS, etc.), database passwords, private keys, Discord webhooks, and more
3. Entropy analysis - Long high-entropy strings in quotes are flagged as potential secrets
4. Email redaction - Regex pattern catches email addresses
5. Custom redaction - You can configure additional strings to redact
6. Tool call redaction - Tool inputs and outputs are redacted with the same standard as regular messages

**This is NOT foolproof.** Always review your exported data before publishing.
Automated redaction cannot catch everything - especially service-specific
identifiers, third-party PII, or secrets in unusual formats.

We recommend converting the exported jsonl into human-readable yaml using `dataclaw jsonl-to-yaml`,
then use tools such as [trufflehog](https://github.com/trufflesecurity/trufflehog) and [gitleaks](https://github.com/gitleaks/gitleaks) to scan it.
You can also compare the exported jsonl with a previous baseline using `dataclaw diff-jsonl`.

To help improve redaction, report issues: https://github.com/banodoco/dataclaw/issues

### Data schema

Each line in `conversations.jsonl` is one session:

```json
{
  "session_id": "abc-123",
  "project": "my-project",
  "model": "claude-opus-4-6",
  "git_branch": "main",
  "start_time": "2025-06-15T10:00:00+00:00",
  "end_time": "2025-06-15T10:30:00+00:00",
  "messages": [
    {
      "role": "user",
      "content": "Fix the login bug",
      "content_parts": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
      ],
      "timestamp": "..."
    },
    {
      "role": "assistant",
      "content": "I'll investigate the login flow.",
      "thinking": "The user wants me to look at...",
      "tool_uses": [
          {
            "tool": "bash",
            "input": {"command": "grep -r 'login' src/"},
            "output": {
              "text": "src/auth.py:42: def login(user, password):",
              "raw": {"stderr": "", "interrupted": false}
            },
            "status": "success"
          }
        ],
      "timestamp": "..."
    }
  ],
  "stats": {
    "user_messages": 5, "assistant_messages": 8,
    "tool_uses": 20, "input_tokens": 50000, "output_tokens": 3000
  }
}
```

`messages[].content_parts` is optional and preserves structured user content such as attachments when the source provides them. The canonical human-readable user text remains in `messages[].content`.

`tool_uses[].output.raw` is optional and preserves extra structured tool-result fields when the source provides them. The canonical human-readable result text remains in `tool_uses[].output.text`.

Each HF repo also includes a `metadata.json` with aggregate stats.

## Finding datasets on Hugging Face

All repos are tagged `dataclaw`.

- **Browse all:** [huggingface.co/datasets?other=dataclaw](https://huggingface.co/datasets?other=dataclaw)
- **Load one:**
  ```python
  from datasets import load_dataset
  ds = load_dataset("alice/my-personal-codex-data", split="train")
  ```
- **Combine several:**
  ```python
  from datasets import load_dataset, concatenate_datasets
  repos = ["alice/my-personal-codex-data", "bob/my-personal-codex-data"]
  ds = concatenate_datasets([load_dataset(r, split="train") for r in repos])
  ```

The auto-generated HF README includes:
- Model distribution (which models, how many sessions each)
- Total token counts
- Project count
- Last updated timestamp

## Contributing

**Missing data:** If you found any data not exported, please report an issue. You can ask your coding agent to analyze the data, export it in this repo, and open a PR.

**Better scheme:** If you need to clean the data and want to propose a better scheme, feel free to open an issue.

**New provider:** If you use a new coding agent, you can ask it to read this repo and export its data as a new provider. Take Claude Code and Codex parsers as examples because they are the most well maintained. When you finish, ask the following questions:
- Did you follow the scheme above? Currently it's free to add custom fields in `messages[].content_parts` and `tool_uses[].output.raw`.
- Did you export all data, especially:
  - tool call inputs and outputs
  - long inputs and outputs that may be saved somewhere else
  - binary content (may be encoded as base64) such as images. We do not apply anonymizer on binary content
  - subagents
- Does the coding agent automatically delete old sessions? How to prevent this?

## Code Quality

<p align="center">
  <img src="scorecard.png" alt="Code Quality Scorecard">
</p>

## License

MIT
