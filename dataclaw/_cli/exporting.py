"""Export and publish helpers for the DataClaw CLI."""

import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .. import _json as json
from ..anonymizer import Anonymizer
from ..secrets import redact_session
from .common import HF_TAG, REPO_URL, SKILL_URL, _format_token_count, _provider_dataset_tags


def export_to_jsonl(
    selected_projects: list[dict],
    output_path: Path,
    anonymizer: Anonymizer,
    parse_project_sessions_fn,
    default_source: str,
    include_thinking: bool = True,
    custom_strings: list[str] | None = None,
) -> dict:
    total = 0
    skipped = 0
    total_redactions = 0
    models: dict[str, int] = {}
    total_input_tokens = 0
    total_output_tokens = 0
    project_names = []

    try:
        fh = open(output_path, "wb")
    except OSError as e:
        print(f"Error: cannot write to {output_path}: {e}", file=sys.stderr)
        sys.exit(1)

    with fh as f:
        for project in selected_projects:
            print(f"  Parsing {project['display_name']}...", end="", flush=True)
            sessions = parse_project_sessions_fn(
                project["dir_name"],
                anonymizer=anonymizer,
                include_thinking=include_thinking,
                source=project.get("source", default_source),
            )
            proj_count = 0
            for session in sessions:
                model = session.get("model")
                if not model or model == "<synthetic>":
                    skipped += 1
                    continue

                session, n_redacted = redact_session(session, custom_strings=custom_strings)
                total_redactions += n_redacted

                f.write(json.dumps_bytes(session))
                f.write(b"\n")
                total += 1
                proj_count += 1
                models[model] = models.get(model, 0) + 1
                stats = session.get("stats", {})
                total_input_tokens += stats.get("input_tokens", 0)
                total_output_tokens += stats.get("output_tokens", 0)
            if proj_count:
                project_names.append(project["display_name"])
            print(f" {proj_count} sessions")

    return {
        "sessions": total,
        "skipped": skipped,
        "redactions": total_redactions,
        "models": models,
        "projects": project_names,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def push_to_huggingface(jsonl_path: Path, repo_id: str, meta: dict) -> None:
    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("Error: huggingface_hub not installed. Run: pip install huggingface_hub", file=sys.stderr)
        sys.exit(1)

    api = HfApi()

    try:
        user_info = api.whoami()
        print(f"Logged in as: {user_info['name']}")
    except (OSError, KeyError, ValueError) as e:
        print(f"Error: Not logged in to Hugging Face ({e}).", file=sys.stderr)
        print("Run: huggingface-cli login", file=sys.stderr)
        sys.exit(1)

    print(f"Pushing to: {repo_id}")
    try:
        api.create_repo(repo_id, repo_type="dataset", exist_ok=True)

        api.upload_file(
            path_or_fileobj=str(jsonl_path),
            path_in_repo="conversations.jsonl",
            repo_id=repo_id,
            repo_type="dataset",
            commit_message="Update conversation data",
        )

        api.upload_file(
            path_or_fileobj=json.dumps_bytes(meta, indent=2),
            path_in_repo="metadata.json",
            repo_id=repo_id,
            repo_type="dataset",
            commit_message="Update metadata",
        )

        api.upload_file(
            path_or_fileobj=_build_dataset_card(repo_id, meta).encode(),
            path_in_repo="README.md",
            repo_id=repo_id,
            repo_type="dataset",
            commit_message="Update dataset card",
        )
    except (OSError, ValueError) as e:
        print(f"Error uploading to Hugging Face: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\nDataset: https://huggingface.co/datasets/{repo_id}")
    print(f"Browse all: https://huggingface.co/datasets?other={HF_TAG}")


def _build_dataset_card(repo_id: str, meta: dict) -> str:
    models = meta.get("models", {})
    sessions = meta.get("sessions", 0)
    projects = meta.get("projects", [])
    total_input = meta.get("total_input_tokens", 0)
    total_output = meta.get("total_output_tokens", 0)
    timestamp = meta.get("exported_at", "")[:10]

    model_tags = "\n".join(f"  - {m}" for m in sorted(models.keys()) if m != "unknown")
    model_lines = "\n".join(f"| {m} | {c} |" for m, c in sorted(models.items(), key=lambda x: -x[1]))

    return f"""---
license: mit
task_categories:
  - text-generation
language:
  - en
tags:
  - dataclaw
{_provider_dataset_tags()}
  - conversations
  - coding-assistant
  - tool-use
  - agentic-coding
{model_tags}
pretty_name: Coding Agent Conversations
configs:
  - config_name: default
    data_files: conversations.jsonl
---

# Coding Agent Conversation Logs

> **This is a performance art project.** Anthropic built their models on the world's freely shared information, then introduced increasingly [dystopian data policies](https://www.anthropic.com/news/detecting-and-preventing-distillation-attacks) to stop anyone else from doing the same with their data — pulling up the ladder behind them. DataClaw lets you throw the ladder back down. The dataset it produces is yours to share.

Exported with [DataClaw]({REPO_URL}).

**Tag: `dataclaw`** — [Browse all DataClaw datasets](https://huggingface.co/datasets?other=dataclaw)

## Stats

| Metric | Value |
|--------|-------|
| Sessions | {sessions} |
| Projects | {len(projects)} |
| Input tokens | {_format_token_count(total_input)} |
| Output tokens | {_format_token_count(total_output)} |
| Last updated | {timestamp} |

### Models

| Model | Sessions |
|-------|----------|
{model_lines}

## Schema

Each line in `conversations.jsonl` is one conversation session:

```json
{{
  "session_id": "uuid",
  "project": "my-project",
  "model": "gpt-5.3-codex",
  "git_branch": "main",
  "start_time": "2025-01-15T10:00:00+00:00",
  "end_time": "2025-01-15T10:30:00+00:00",
  "messages": [
    {{"role": "user", "content": "Fix the login bug", "timestamp": "..."}},
    {{
      "role": "assistant",
      "content": "I'll investigate the login flow.",
      "thinking": "The user wants me to...",
      "tool_uses": [
          {{
            "tool": "bash",
            "input": {{"command": "grep -r 'login' src/"}},
            "output": {{"text": "src/auth.py:42: def login(user, password):"}},
            "status": "success"
          }}
        ],
      "timestamp": "..."
    }}
  ],
  "stats": {{
    "user_messages": 5,
    "assistant_messages": 8,
    "tool_uses": 20,
    "input_tokens": 50000,
    "output_tokens": 3000
  }}
}}
```

### Privacy

- Paths anonymized to project-relative; usernames hashed

## Load

```python
from datasets import load_dataset
ds = load_dataset("{repo_id}", split="train")
```

## Export your own

```bash
pip install dataclaw
dataclaw
```
"""


def update_skill(target: str) -> None:
    if target != "claude":
        print(f"Error: unknown target '{target}'. Supported: claude", file=sys.stderr)
        sys.exit(1)

    dest = Path.cwd() / ".claude" / "skills" / "dataclaw" / "SKILL.md"
    dest.parent.mkdir(parents=True, exist_ok=True)

    print(f"Downloading skill from {SKILL_URL}...")
    try:
        with urllib.request.urlopen(SKILL_URL, timeout=15) as resp:
            content = resp.read().decode()
    except (OSError, urllib.error.URLError) as e:
        print(f"Error downloading skill: {e}", file=sys.stderr)
        bundled = Path(__file__).resolve().parent.parent.parent / "docs" / "SKILL.md"
        if bundled.exists():
            print(f"Using bundled copy from {bundled}")
            content = bundled.read_text()
        else:
            print("No bundled copy available either.", file=sys.stderr)
            sys.exit(1)

    dest.write_text(content)
    print(f"Skill installed to {dest}")
    print(
        json.dumps(
            {
                "installed": str(dest),
                "next_steps": ["Run: dataclaw prep"],
                "next_command": "dataclaw prep",
            },
            indent=2,
        )
    )
