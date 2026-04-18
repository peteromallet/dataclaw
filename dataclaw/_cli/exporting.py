"""Export and publish helpers for the DataClaw CLI."""

import hashlib
import json as std_json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .. import _json as json
from ..anonymizer import Anonymizer
from ..secrets import redact_session
from .common import HF_TAG, REPO_URL, SKILL_URL, _format_token_count, _provider_dataset_tags


def _token_totals(stats: object) -> tuple[int, int]:
    if not isinstance(stats, dict):
        return 0, 0
    return stats.get("input_tokens", 0), stats.get("output_tokens", 0)


def _normalize_model_stats_key(key: object) -> str | None:
    if not isinstance(key, str):
        return None

    key = key.strip()
    if not key:
        return None

    key = key.rsplit("/", 1)[-1]
    key = key.replace("_", "-")
    key = key.replace(".", "-")
    return key


def _normalize_project_stats_key(key: object) -> str | None:
    if not isinstance(key, str):
        return None

    key = key.strip()
    if not key:
        return None

    if ":" in key:
        key = key.split(":", 1)[1]

    key = key.lower()
    key = key.replace("_", "-")
    key = key.replace(".", "-")
    return key or None


def _add_breakdown_row(
    breakdown: dict[str, dict[str, int]],
    key: object,
    *,
    input_tokens: int,
    output_tokens: int,
) -> None:
    if not isinstance(key, str) or not key.strip():
        return

    row = breakdown.setdefault(key, {"sessions": 0, "input_tokens": 0, "output_tokens": 0})
    row["sessions"] += 1
    row["input_tokens"] += input_tokens
    row["output_tokens"] += output_tokens


def _gemini_dedupe_fingerprint(session: dict, source: str) -> str | None:
    if source != "gemini":
        return None

    canonical = dict(session)
    canonical["source"] = source
    canonical.pop("project", None)
    payload = std_json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


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
    model_breakdown: dict[str, dict[str, int]] = {}
    project_breakdown: dict[str, dict[str, int]] = {}
    total_input_tokens = 0
    total_output_tokens = 0
    seen_fingerprints: set[str] = set()

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
                source = session.get("source") or project.get("source", default_source)
                model = session.get("model")
                if not model or model == "<synthetic>":
                    skipped += 1
                    continue

                fingerprint = _gemini_dedupe_fingerprint(session, source)
                if fingerprint is not None and fingerprint in seen_fingerprints:
                    continue

                session, n_redacted = redact_session(session, custom_strings=custom_strings)
                total_redactions += n_redacted

                if fingerprint is not None:
                    seen_fingerprints.add(fingerprint)

                f.write(json.dumps_bytes(session))
                f.write(b"\n")
                total += 1
                proj_count += 1
                input_tokens, output_tokens = _token_totals(session.get("stats", {}))
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens
                _add_breakdown_row(
                    model_breakdown,
                    _normalize_model_stats_key(model),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                _add_breakdown_row(
                    project_breakdown,
                    _normalize_project_stats_key(session.get("project") or project["display_name"]),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            print(f" {proj_count} sessions")

    return {
        "sessions": total,
        "skipped": skipped,
        "redactions": total_redactions,
        "model_breakdown": model_breakdown,
        "project_breakdown": project_breakdown,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def summarize_export_jsonl(jsonl_path: Path) -> dict:
    model_breakdown: dict[str, dict[str, int]] = {}
    project_breakdown: dict[str, dict[str, int]] = {}
    total = 0
    total_input_tokens = 0
    total_output_tokens = 0

    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            total += 1

            model = row.get("model")
            project = row.get("project")

            input_tokens, output_tokens = _token_totals(row.get("stats", {}))
            total_input_tokens += input_tokens
            total_output_tokens += output_tokens
            _add_breakdown_row(
                model_breakdown,
                _normalize_model_stats_key(model),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            _add_breakdown_row(
                project_breakdown,
                _normalize_project_stats_key(project),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

    return {
        "sessions": total,
        "model_breakdown": model_breakdown,
        "project_breakdown": project_breakdown,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _normalize_breakdown(
    raw_breakdown: object,
    *,
    normalize_key,
) -> dict[str, dict[str, int]]:
    if not isinstance(raw_breakdown, dict):
        return {}

    normalized: dict[str, dict[str, int]] = {}
    for name, stats in raw_breakdown.items():
        normalized_name = normalize_key(name)
        if normalized_name is None or not isinstance(stats, dict):
            continue
        row = normalized.setdefault(normalized_name, {"sessions": 0, "input_tokens": 0, "output_tokens": 0})
        row["sessions"] += stats.get("sessions", 0)
        row["input_tokens"] += stats.get("input_tokens", 0)
        row["output_tokens"] += stats.get("output_tokens", 0)

    return normalized


def _fallback_breakdown(counts: object, names: object, *, normalize_key) -> dict[str, dict[str, int]]:
    breakdown: dict[str, dict[str, int]] = {}

    if isinstance(counts, dict):
        for name, sessions in counts.items():
            normalized_name = normalize_key(name)
            if normalized_name is None:
                continue
            row = breakdown.setdefault(normalized_name, {"sessions": 0, "input_tokens": 0, "output_tokens": 0})
            row["sessions"] += sessions if isinstance(sessions, int) else 0
        return breakdown

    if isinstance(names, list):
        for name in names:
            normalized_name = normalize_key(name)
            if normalized_name is not None:
                breakdown.setdefault(normalized_name, {"sessions": 0, "input_tokens": 0, "output_tokens": 0})

    return breakdown


def _sorted_breakdown_rows(breakdown: object) -> list[tuple[str, dict[str, int]]]:
    if not isinstance(breakdown, dict):
        return []

    rows: list[tuple[str, dict[str, int]]] = []
    for name, stats in breakdown.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(stats, dict):
            continue
        rows.append(
            (
                name,
                {
                    "sessions": stats.get("sessions", 0),
                    "input_tokens": stats.get("input_tokens", 0),
                    "output_tokens": stats.get("output_tokens", 0),
                },
            )
        )

    return sorted(rows, key=lambda item: (-item[1]["output_tokens"], item[0]))


def _build_breakdown_table(label: str, breakdown: object) -> str:
    rows = _sorted_breakdown_rows(breakdown)
    if not rows:
        return f"| {label} | Sessions | Input tokens | Output tokens |\n|-------|----------|--------------|---------------|\n| None | 0 | 0 | 0 |"

    lines = [
        f"| {label} | Sessions | Input tokens | Output tokens |",
        "|-------|----------|--------------|---------------|",
    ]
    for name, stats in rows:
        lines.append(
            f"| {name} | {stats['sessions']} | {_format_token_count(stats['input_tokens'])} | {_format_token_count(stats['output_tokens'])} |"
        )
    return "\n".join(lines)


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
        print("Run: hf auth login --token <YOUR_TOKEN>", file=sys.stderr)
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
    sessions = meta.get("sessions", 0)
    model_breakdown = _normalize_breakdown(meta.get("model_breakdown"), normalize_key=_normalize_model_stats_key)
    if not model_breakdown:
        model_breakdown = _fallback_breakdown(meta.get("models"), None, normalize_key=_normalize_model_stats_key)
    project_breakdown = _normalize_breakdown(meta.get("project_breakdown"), normalize_key=_normalize_project_stats_key)
    if not project_breakdown:
        project_breakdown = _fallback_breakdown(None, meta.get("projects"), normalize_key=_normalize_project_stats_key)
    total_input = meta.get("total_input_tokens", 0)
    total_output = meta.get("total_output_tokens", 0)
    timestamp = meta.get("exported_at", "")[:10]

    model_tags = "\n".join(f"  - {m}" for m, _stats in _sorted_breakdown_rows(model_breakdown) if m != "unknown")
    model_table = _build_breakdown_table("Model", model_breakdown)
    project_table = _build_breakdown_table("Project", project_breakdown)

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

> **This is a performance art project.** Anthropic built their models on the world's freely shared information, then introduced increasingly [dystopian data policies](https://www.anthropic.com/news/detecting-and-preventing-distillation-attacks) to stop anyone else from doing the same with their data - pulling up the ladder behind them. DataClaw lets you throw the ladder back down. The dataset it produces is yours to share.

Exported with [DataClaw]({REPO_URL}).

**Tag: `dataclaw`** - [Browse all DataClaw datasets](https://huggingface.co/datasets?other=dataclaw)

## Stats

| Metric | Value |
|--------|-------|
| Sessions | {sessions} |
| Projects | {len(project_breakdown)} |
| Input tokens | {_format_token_count(total_input)} |
| Output tokens | {_format_token_count(total_output)} |
| Last updated | {timestamp} |

### Models

{model_table}

### Projects

{project_table}

## Schema

Each line in `conversations.jsonl` is one session:

```json
{{
  "session_id": "abc-123",
  "project": "my-project",
  "model": "claude-opus-4-6",
  "git_branch": "main",
  "start_time": "2025-06-15T10:00:00+00:00",
  "end_time": "2025-06-15T10:30:00+00:00",
  "messages": [
    {{
      "role": "user",
      "content": "Fix the login bug",
      "content_parts": [
        {{"type": "image", "source": {{"type": "base64", "media_type": "image/png", "data": "..."}}}}
      ],
      "timestamp": "..."
    }},
    {{
      "role": "assistant",
      "content": "I'll investigate the login flow.",
      "thinking": "The user wants me to look at...",
      "tool_uses": [
          {{
            "tool": "bash",
            "input": {{"command": "grep -r 'login' src/"}},
            "output": {{
              "text": "src/auth.py:42: def login(user, password):",
              "raw": {{"stderr": "", "interrupted": false}}
            }},
            "status": "success"
          }}
        ],
      "timestamp": "..."
    }}
  ],
  "stats": {{
    "user_messages": 5, "assistant_messages": 8,
    "tool_uses": 20, "input_tokens": 50000, "output_tokens": 3000
  }}
}}
```

`messages[].content_parts` is optional and preserves structured user content such as attachments when the source provides them. The canonical human-readable user text remains in `messages[].content`.

`tool_uses[].output.raw` is optional and preserves extra structured tool-result fields when the source provides them. The canonical human-readable result text remains in `tool_uses[].output.text`.

## Load

```python
from datasets import load_dataset
ds = load_dataset("{repo_id}", split="train")
```

## Export your own

```bash
pip install -U dataclaw
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
            content = resp.read()
    except (OSError, urllib.error.URLError) as e:
        print(f"Error downloading skill: {e}", file=sys.stderr)
        bundled = Path(__file__).resolve().parent.parent.parent / ".claude" / "skills" / "dataclaw" / "SKILL.md"
        if bundled.exists():
            print(f"Using bundled copy from {bundled}")
            content = bundled.read_bytes()
        else:
            print("No bundled copy available either.", file=sys.stderr)
            sys.exit(1)

    dest.write_bytes(content)
    print(f"Skill installed to {dest}")
    print(
        json.dumps(
            {
                "installed": str(dest),
                "next_steps": ["Step 3 - Prep: run dataclaw prep"],
                "next_command": "dataclaw prep",
            },
            indent=2,
        )
    )
