# DataClaw

Turn your Claude Code conversation history into structured training data and publish it to Hugging Face with a single command. DataClaw parses session logs, redacts secrets and PII, and uploads the result as a ready-to-use dataset — so the hours you spend coding with Claude can help make future models better.

![DataClaw](dataclaw.jpeg)

Every dataset is tagged **`dataclaw`** on Hugging Face. Individually, each export is a snapshot of one person's coding sessions. But collectively, they form a growing distributed dataset of real-world human-AI collaboration — the kind of data that's hard to find and expensive to create. The more people share, the more useful the dataset becomes for everyone building the next generation of coding tools.

[Browse all DataClaw datasets](https://huggingface.co/datasets?other=dataclaw)

## Getting started

Paste this into Claude Code (or any coding agent):

```
Help me export my Claude Code conversation history to Hugging Face using DataClaw.
Install it, set up the skill, then walk me through the process.

STEP 1 — INSTALL
  pip install dataclaw
  If that fails: git clone https://github.com/banodoco/dataclaw.git /tmp/dataclaw && pip install /tmp/dataclaw
  If that also fails, ask the user where the source is.

STEP 2 — INSTALL SKILL
  dataclaw update-skill claude

STEP 3 — START
  dataclaw prep
  Every dataclaw command outputs next_steps in its JSON — follow them through the entire flow.

IMPORTANT: Never run bare `huggingface-cli login` — always use --token.
IMPORTANT: Always export with --no-push first and review for PII before publishing.
```

<details>
<summary><b>Manual usage (without an agent)</b></summary>

### Quick start

```bash
pip install dataclaw
huggingface-cli login --token YOUR_TOKEN

# See your projects
dataclaw prep

# Configure
dataclaw config --repo username/dataclaw-username
dataclaw config --exclude "personal-stuff,scratch"
dataclaw config --redact-usernames "my_github_handle,my_discord_name"
dataclaw config --redact "my-domain.com,my-secret-project"

# Export locally first
dataclaw export --no-push

# Review and confirm
dataclaw confirm

# Push
dataclaw export
```

### Commands

| Command | Description |
|---------|-------------|
| `dataclaw status` | Show current stage and next steps (JSON) |
| `dataclaw prep` | Discover projects, check HF auth, output JSON |
| `dataclaw list` | List all projects with exclusion status |
| `dataclaw config` | Show current config |
| `dataclaw config --repo user/dataclaw-user` | Set HF repo |
| `dataclaw config --exclude "a,b"` | Add excluded projects (appends) |
| `dataclaw config --redact "str1,str2"` | Add strings to always redact (appends) |
| `dataclaw config --redact-usernames "u1,u2"` | Add usernames to anonymize (appends) |
| `dataclaw config --confirm-projects` | Mark project selection as confirmed |
| `dataclaw export --no-push` | Export locally only (always do this first) |
| `dataclaw confirm` | Scan for PII, summarize export, unlock pushing |
| `dataclaw export` | Export and push (requires `dataclaw confirm` first) |
| `dataclaw export --all-projects` | Include everything (ignore exclusions) |
| `dataclaw export --no-thinking` | Exclude extended thinking blocks |
| `dataclaw update-skill claude` | Install/update the dataclaw skill for Claude Code |

</details>

<details>
<summary><b>What gets exported</b></summary>

| Data | Included | Notes |
|------|----------|-------|
| User messages | Yes | Full text (including voice transcripts) |
| Assistant responses | Yes | Full text output |
| Extended thinking | Yes | Claude's reasoning (opt out with `--no-thinking`) |
| Tool calls | Yes | Tool name + summarized input |
| Tool results | No | Not stored in Claude Code's logs |
| Token usage | Yes | Input/output tokens per session |
| Model & metadata | Yes | Model name, git branch, timestamps |

### Privacy & Redaction

DataClaw applies multiple layers of protection:

1. **Path anonymization** — File paths stripped to project-relative
2. **Username hashing** — Your macOS username + any configured usernames replaced with stable hashes
3. **Secret detection** — Regex patterns catch JWT tokens, API keys (Anthropic, OpenAI, HF, GitHub, AWS, etc.), database passwords, private keys, Discord webhooks, and more
4. **Entropy analysis** — Long high-entropy strings in quotes are flagged as potential secrets
5. **Email redaction** — Personal email addresses removed
6. **Custom redaction** — You can configure additional strings and usernames to redact
7. **Tool input pre-redaction** — Secrets in tool inputs are redacted BEFORE truncation to prevent partial leaks

**This is NOT foolproof.** Always review your exported data before publishing.
Automated redaction cannot catch everything — especially service-specific
identifiers, third-party PII, or secrets in unusual formats.

To help improve redaction, report issues: https://github.com/banodoco/dataclaw/issues

</details>

<details>
<summary><b>Data schema</b></summary>

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
    {"role": "user", "content": "Fix the login bug", "timestamp": "..."},
    {
      "role": "assistant",
      "content": "I'll investigate the login flow.",
      "thinking": "The user wants me to look at...",
      "tool_uses": [{"tool": "Read", "input": "src/auth.py"}],
      "timestamp": "..."
    }
  ],
  "stats": {
    "user_messages": 5, "assistant_messages": 8,
    "tool_uses": 20, "input_tokens": 50000, "output_tokens": 3000
  }
}
```

Each HF repo also includes a `metadata.json` with aggregate stats.

</details>

<details>
<summary><b>Finding datasets on Hugging Face</b></summary>

All repos are named `{username}/dataclaw-{username}` and tagged `dataclaw`.

- **Browse all:** [huggingface.co/datasets?other=dataclaw](https://huggingface.co/datasets?other=dataclaw)
- **Load one:**
  ```python
  from datasets import load_dataset
  ds = load_dataset("alice/dataclaw-alice", split="train")
  ```
- **Combine several:**
  ```python
  from datasets import load_dataset, concatenate_datasets
  repos = ["alice/dataclaw-alice", "bob/dataclaw-bob"]
  ds = concatenate_datasets([load_dataset(r, split="train") for r in repos])
  ```

The auto-generated HF README includes:
- Model distribution (which Claude models, how many sessions each)
- Total token counts
- Project count
- Last updated timestamp

</details>

## Code Quality

<p align="center">
  <img src="scorecard.png" alt="Code Quality Scorecard">
</p>

## License

MIT
