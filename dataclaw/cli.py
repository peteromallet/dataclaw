"""CLI for DataClaw — export Claude Code conversations to Hugging Face."""

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .parser import AnonymizerWrapper
from .config import CONFIG_FILE, DataClawConfig, load_config, save_config
from .parser import CLAUDE_DIR, discover_projects, parse_project_sessions
from .secrets import redact_session
from .search import SEARCH_DB_PATH, build_index, search as do_search, get_index_stats

HF_TAG = "dataclaw"
REPO_URL = "https://github.com/banodoco/dataclaw"
SKILL_URL = "https://raw.githubusercontent.com/banodoco/dataclaw/main/docs/SKILL.md"


def _mask_secret(s: str) -> str:
    """Mask a secret string for display, e.g. 'hf_OOgd...oEVH'."""
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}...{s[-4:]}"


def _mask_config_for_display(config: dict) -> dict:
    """Return a copy of config with redact_strings values masked."""
    out = dict(config)
    if out.get("redact_strings"):
        out["redact_strings"] = [_mask_secret(s) for s in out["redact_strings"]]
    return out


def _format_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{size_bytes} B"
        size_bytes /= 1024
    return f"{size_bytes:.1f} GB"


def _format_token_count(count: int) -> str:
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.1f}B"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}K"
    return str(count)


def get_hf_username() -> str | None:
    """Get the currently logged-in HF username, or None."""
    try:
        from huggingface_hub import HfApi
        return HfApi().whoami()["name"]
    except ImportError:
        return None
    except (OSError, KeyError, ValueError):
        return None


def default_repo_name(hf_username: str) -> str:
    """Standard repo name: {username}/my-personal-claude-code-data"""
    return f"{hf_username}/my-personal-claude-code-data"


def _compute_stage(config: DataClawConfig) -> tuple[str, int, str | None]:
    """Return (stage_name, stage_number, hf_username)."""
    hf_user = get_hf_username()
    if not hf_user:
        return ("auth", 1, None)
    saved = config.get("stage")
    last_export = config.get("last_export")
    if saved == "done" and last_export:
        return ("done", 4, hf_user)
    if saved == "confirmed" and last_export:
        return ("confirmed", 3, hf_user)
    if saved == "review" and last_export:
        return ("review", 3, hf_user)
    return ("configure", 2, hf_user)


def _build_status_next_steps(
    stage: str, config: DataClawConfig, hf_user: str | None, repo_id: str | None,
) -> tuple[list[str], str | None]:
    """Return (next_steps, next_command) for the given stage."""
    if stage == "auth":
        return (
            [
                "Ask the user for their Hugging Face token. Sign up: https://huggingface.co/join — Create WRITE token: https://huggingface.co/settings/tokens",
                "Run: huggingface-cli login --token <THEIR_TOKEN> (NEVER run bare huggingface-cli login — it hangs)",
                "Run: dataclaw config --redact \"<THEIR_TOKEN>\" (so the token gets redacted from exports)",
                "Run: dataclaw prep (to confirm login and get next steps)",
            ],
            None,
        )

    if stage == "configure":
        projects_confirmed = config.get("projects_confirmed", False)
        steps = []
        if not projects_confirmed:
            steps.append(
                "Run: dataclaw prep — then show the user their projects list and ask which to EXCLUDE. "
                "Configure: dataclaw config --exclude \"project1,project2\" "
                "or dataclaw config --confirm-projects (to include all)"
            )
        steps.extend([
            "Ask about GitHub/Discord usernames to anonymize and sensitive strings to redact. "
            "Configure: dataclaw config --redact-usernames \"handle1\" and dataclaw config --redact \"string1\"",
            "When done configuring, export locally: dataclaw export --no-push --output /tmp/dataclaw_export.jsonl",
        ])
        # next_command is null because user input is needed before exporting
        return (steps, None)

    if stage == "review":
        return (
            [
                "Ask the user: 'What is your full name?' Then scan the export for it.",
                "Run PII scan commands and review results with the user.",
                "Ask the user: 'Are there any company names, internal project names, client names, private URLs, or other people's names in your conversations that you'd want redacted? Any custom domains or internal tools?' Add anything they mention with dataclaw config --redact.",
                "Do a deep manual scan: sample ~20 sessions from the export (beginning, middle, end) and scan for names, private URLs, company names, credentials in conversation text, and anything else that looks sensitive. Report findings to the user.",
                "If PII found in any of the above, add redactions (dataclaw config --redact) and re-export: dataclaw export --no-push",
                "Run: dataclaw confirm — scans for PII, shows the project breakdown, and unlocks pushing.",
                "Do NOT push until the user explicitly confirms. Once confirmed, push: dataclaw export",
            ],
            "dataclaw confirm",
        )

    if stage == "confirmed":
        return (
            [
                "User has reviewed the export. Ask: 'Ready to publish to Hugging Face?' and push: dataclaw export",
            ],
            "dataclaw export",
        )

    # done
    dataset_url = f"https://huggingface.co/datasets/{repo_id}" if repo_id else None
    return (
        [
            f"Done! Dataset is live{f' at {dataset_url}' if dataset_url else ''}. To update later: dataclaw export",
            "To reconfigure: dataclaw prep then dataclaw config",
        ],
        None,
    )


def list_projects(claude_dir: Path | None = None) -> None:
    """Print all projects as JSON (for agents to parse)."""
    projects = discover_projects(claude_dir=claude_dir)
    if not projects:
        print("No Claude Code sessions found.")
        return
    config = load_config()
    excluded = set(config.get("excluded_projects", []))
    print(json.dumps(
        [{"name": p["display_name"], "sessions": p["session_count"],
          "size": _format_size(p["total_size_bytes"]),
          "excluded": p["display_name"] in excluded}
         for p in projects],
        indent=2,
    ))


def _merge_config_list(config: DataClawConfig, key: str, new_values: list[str]) -> None:
    """Append new_values to a config list (deduplicated, sorted)."""
    existing = set(config.get(key, []))
    existing.update(new_values)
    config[key] = sorted(existing)


def configure(
    repo: str | None = None,
    exclude: list[str] | None = None,
    redact: list[str] | None = None,
    redact_usernames: list[str] | None = None,
    confirm_projects: bool = False,
):
    """Set config values non-interactively. Lists are MERGED (append), not replaced."""
    config = load_config()
    if repo is not None:
        config["repo"] = repo
    if exclude is not None:
        _merge_config_list(config, "excluded_projects", exclude)
    if redact is not None:
        _merge_config_list(config, "redact_strings", redact)
    if redact_usernames is not None:
        _merge_config_list(config, "redact_usernames", redact_usernames)
    if confirm_projects:
        config["projects_confirmed"] = True
    save_config(config)
    print(f"Config saved to {CONFIG_FILE}")
    print(json.dumps(_mask_config_for_display(config), indent=2))


def export_to_jsonl(
    selected_projects: list[dict],
    output_path: Path,
    anonymizer: AnonymizerWrapper,
    include_thinking: bool = True,
    custom_strings: list[str] | None = None,
) -> dict:
    """Export selected projects to JSONL. Returns metadata."""
    total = 0
    skipped = 0
    total_redactions = 0
    models: dict[str, int] = {}
    total_input_tokens = 0
    total_output_tokens = 0
    project_names = []

    try:
        fh = open(output_path, "w")
    except OSError as e:
        print(f"Error: cannot write to {output_path}: {e}", file=sys.stderr)
        sys.exit(1)

    with fh as f:
        for project in selected_projects:
            print(f"  Parsing {project['display_name']}...", end="", flush=True)
            sessions = parse_project_sessions(
                project["dir_name"], anonymizer=anonymizer,
                include_thinking=include_thinking,
            )
            proj_count = 0
            for session in sessions:
                model = session.get("model")
                if not model or model == "<synthetic>":
                    skipped += 1
                    continue

                session, n_redacted = redact_session(session, custom_strings=custom_strings)
                total_redactions += n_redacted

                f.write(json.dumps(session, ensure_ascii=False) + "\n")
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
    """Push JSONL + metadata to HF dataset repo."""
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
            repo_id=repo_id, repo_type="dataset",
            commit_message="Update conversation data",
        )

        api.upload_file(
            path_or_fileobj=json.dumps(meta, indent=2).encode(),
            path_in_repo="metadata.json",
            repo_id=repo_id, repo_type="dataset",
            commit_message="Update metadata",
        )

        api.upload_file(
            path_or_fileobj=_build_dataset_card(repo_id, meta).encode(),
            path_in_repo="README.md",
            repo_id=repo_id, repo_type="dataset",
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
    model_lines = "\n".join(
        f"| {m} | {c} |" for m, c in sorted(models.items(), key=lambda x: -x[1])
    )

    return f"""---
license: mit
task_categories:
  - text-generation
language:
  - en
tags:
  - dataclaw
  - claude-code
  - conversations
  - coding-assistant
  - tool-use
  - agentic-coding
{model_tags}
pretty_name: Claude Code Conversations
---

# Claude Code Conversation Logs

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
  "model": "claude-opus-4-6",
  "git_branch": "main",
  "start_time": "2025-01-15T10:00:00+00:00",
  "end_time": "2025-01-15T10:30:00+00:00",
  "messages": [
    {{"role": "user", "content": "Fix the login bug", "timestamp": "..."}},
    {{
      "role": "assistant",
      "content": "I'll investigate the login flow.",
      "thinking": "The user wants me to...",
      "tool_uses": [{{"tool": "Read", "input": "src/auth.py"}}],
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
- No tool outputs — only tool call inputs (summaries)

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
    """Download and install the dataclaw skill for a coding agent."""
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
        # Fall back to bundled copy
        bundled = Path(__file__).resolve().parent.parent / "docs" / "SKILL.md"
        if bundled.exists():
            print(f"Using bundled copy from {bundled}")
            content = bundled.read_text()
        else:
            print("No bundled copy available either.", file=sys.stderr)
            sys.exit(1)

    dest.write_text(content)
    print(f"Skill installed to {dest}")
    print(json.dumps({
        "installed": str(dest),
        "next_steps": ["Run: dataclaw prep"],
        "next_command": "dataclaw prep",
    }, indent=2))


def status() -> None:
    """Show current stage and next steps (JSON). Read-only — does not modify config."""
    config = load_config()
    stage, stage_number, hf_user = _compute_stage(config)

    repo_id = config.get("repo")
    if not repo_id and hf_user:
        repo_id = default_repo_name(hf_user)

    next_steps, next_command = _build_status_next_steps(stage, config, hf_user, repo_id)

    result = {
        "stage": stage,
        "stage_number": stage_number,
        "total_stages": 4,
        "hf_logged_in": hf_user is not None,
        "hf_username": hf_user,
        "repo": repo_id,
        "projects_confirmed": config.get("projects_confirmed", False),
        "last_export": config.get("last_export"),
        "next_steps": next_steps,
        "next_command": next_command,
    }
    print(json.dumps(result, indent=2))


def _find_export_file(file_path: Path | None) -> Path:
    """Resolve the export file path, or exit with an error."""
    if file_path and file_path.exists():
        return file_path
    if file_path is None:
        for c in [Path("/tmp/dataclaw_export.jsonl"), Path("dataclaw_conversations.jsonl")]:
            if c.exists():
                return c
    print(json.dumps({
        "error": "No export file found. Run: dataclaw export --no-push --output /tmp/dataclaw_export.jsonl",
    }))
    sys.exit(1)


def _scan_pii(file_path: Path) -> dict:
    """Run PII regex scans on the export file. Returns dict of findings."""
    import re
    import subprocess

    p = str(file_path.resolve())
    scans = {
        "emails": r'[a-zA-Z0-9.+-]+@[a-zA-Z0-9.-]+\.[a-z]{2,}',
        "jwt_tokens": r'eyJ[A-Za-z0-9_-]{20,}',
        "api_keys": r'(ghp_|sk-|hf_)[A-Za-z0-9_-]{10,}',
        "ip_addresses": r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}',
    }
    # Known false positives
    fp_emails = {"noreply", "pytest.fixture", "mcp.tool", "mcp.resource",
                 "server.tool", "tasks.loop", "github.com"}
    fp_keys = {"sk-notification"}

    results = {}
    try:
        content = file_path.read_text(errors="replace")
    except OSError:
        return {}

    for name, pattern in scans.items():
        matches = set(re.findall(pattern, content))
        # Filter false positives
        if name == "emails":
            matches = {m for m in matches if not any(fp in m for fp in fp_emails)}
        if name == "api_keys":
            matches = {m for m in matches if m not in fp_keys}
        if matches:
            results[name] = sorted(matches)[:20]  # cap at 20

    return results


def confirm(file_path: Path | None = None) -> None:
    """Scan export for PII, summarize projects, and unlock pushing. JSON output."""
    config = load_config()
    last_export = config.get("last_export", {})
    file_path = _find_export_file(file_path)

    # Read and summarize
    projects: dict[str, int] = {}
    models: dict[str, int] = {}
    total = 0
    try:
        with open(file_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                total += 1
                proj = row.get("project", "<unknown>")
                projects[proj] = projects.get(proj, 0) + 1
                model = row.get("model", "<unknown>")
                models[model] = models.get(model, 0) + 1
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"Cannot read {file_path}: {e}"}))
        sys.exit(1)

    file_size = file_path.stat().st_size
    repo_id = config.get("repo")

    # Run PII scans
    pii_findings = _scan_pii(file_path)

    # Advance stage from review -> confirmed
    config["stage"] = "confirmed"
    save_config(config)

    next_steps = [
        "Show the user the project breakdown and PII scan results above.",
    ]
    if pii_findings:
        next_steps.append(
            "PII findings detected — review each one with the user. "
            "If real: dataclaw config --redact \"string\" then re-export with --no-push. "
            "False positives can be ignored."
        )
    next_steps.extend([
        "If any project should be excluded, run: dataclaw config --exclude \"project_name\" and re-export with --no-push.",
        f"This will publish {total} sessions ({_format_size(file_size)}) publicly to Hugging Face"
        + (f" at {repo_id}" if repo_id else "") + ". Ask the user: 'Are you ready to proceed?'",
        "Once confirmed, push: dataclaw export",
    ])

    result = {
        "stage": "confirmed",
        "stage_number": 3,
        "total_stages": 4,
        "file": str(file_path.resolve()),
        "file_size": _format_size(file_size),
        "total_sessions": total,
        "projects": [
            {"name": name, "sessions": count}
            for name, count in sorted(projects.items(), key=lambda x: -x[1])
        ],
        "models": {m: c for m, c in sorted(models.items(), key=lambda x: -x[1])},
        "pii_scan": pii_findings if pii_findings else "clean",
        "repo": repo_id,
        "last_export_timestamp": last_export.get("timestamp"),
        "next_steps": next_steps,
        "next_command": "dataclaw export",
    }
    print(json.dumps(result, indent=2))


def prep(claude_dir: Path | None = None) -> None:
    """Data prep — discover projects, detect HF auth, output JSON.

    Designed to be called by an agent which handles the interactive parts.
    Outputs pure JSON to stdout so agents can parse it directly.
    """
    # Use provided claude_dir or fall back to default
    effective_claude_dir = claude_dir or CLAUDE_DIR
    
    if not effective_claude_dir.exists():
        print(json.dumps({"error": "~/.claude not found. Is Claude Code installed?"}))
        sys.exit(1)

    projects = discover_projects(claude_dir=effective_claude_dir)
    if not projects:
        print(json.dumps({"error": "No Claude Code sessions found."}))
        sys.exit(1)

    config = load_config()
    excluded = set(config.get("excluded_projects", []))

    # Use _compute_stage to determine where we are
    stage, stage_number, hf_user = _compute_stage(config)

    repo_id = config.get("repo")
    if not repo_id and hf_user:
        repo_id = default_repo_name(hf_user)

    # Build contextual next_steps
    next_steps, next_command = _build_status_next_steps(stage, config, hf_user, repo_id)

    # Persist stage
    config["stage"] = stage
    save_config(config)

    result = {
        "stage": stage,
        "stage_number": stage_number,
        "total_stages": 4,
        "next_command": next_command,
        "hf_logged_in": hf_user is not None,
        "hf_username": hf_user,
        "repo": repo_id,
        "projects": [
            {
                "name": p["display_name"],
                "sessions": p["session_count"],
                "size": _format_size(p["total_size_bytes"]),
                "excluded": p["display_name"] in excluded,
            }
            for p in projects
        ],
        "redact_strings": [_mask_secret(s) for s in config.get("redact_strings", [])],
        "redact_usernames": config.get("redact_usernames", []),
        "config_file": str(CONFIG_FILE),
        "next_steps": next_steps,
    }
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="DataClaw — Claude Code -> Hugging Face")
    parser.add_argument(
        "--claude-dir", 
        type=Path, 
        default=None,
        help="Path to Claude Code directory (default: ~/.claude or CLAUDE_DIR env var)"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("prep", help="Data prep — discover projects, detect HF, output JSON")
    sub.add_parser("status", help="Show current stage and next steps (JSON)")
    cf = sub.add_parser("confirm", help="Scan for PII, summarize export, and unlock pushing (JSON)")
    cf.add_argument("--file", "-f", type=Path, default=None, help="Path to export JSONL file")
    sub.add_parser("list", help="List all projects")

    us = sub.add_parser("update-skill", help="Install/update the dataclaw skill for a coding agent")
    us.add_argument("target", choices=["claude"], help="Agent to install skill for")

    cfg = sub.add_parser("config", help="View or set config")
    cfg.add_argument("--repo", type=str, help="Set HF repo")
    cfg.add_argument("--exclude", type=str, help="Comma-separated projects to exclude")
    cfg.add_argument("--redact", type=str,
                     help="Comma-separated strings to always redact (API keys, usernames, domains)")
    cfg.add_argument("--redact-usernames", type=str,
                     help="Comma-separated usernames to anonymize (GitHub handles, Discord names)")
    cfg.add_argument("--confirm-projects", action="store_true",
                     help="Mark project selection as confirmed (include all)")

    exp = sub.add_parser("export", help="Export and push (default)")
    # Export flags on both the subcommand and root parser so `dataclaw --no-push` works
    for target in (exp, parser):
        target.add_argument("--output", "-o", type=Path, default=None)
        target.add_argument("--repo", "-r", type=str, default=None)
        target.add_argument("--all-projects", action="store_true")
        target.add_argument("--no-thinking", action="store_true")
        target.add_argument("--no-push", action="store_true")

    # Search subcommands
    idx = sub.add_parser("index", help="Build search index from Claude Code sessions")
    idx.add_argument("--projects", type=str, help="Comma-separated projects to index (default: all)")
    idx.add_argument("--force", action="store_true", help="Rebuild index from scratch")

    sch = sub.add_parser("search", help="Search indexed sessions")
    sch.add_argument("query", type=str, help="Search query string")
    sch.add_argument("--limit", "-n", type=int, default=20, help="Max results (default: 20)")
    sch.add_argument("--min-confidence", "-c", type=int, default=0,
                     help="Minimum confidence score 0-100 (default: 0)")
    sch.add_argument("--json", action="store_true",
                     help="Output results as JSON only (no table formatting)")
    sch.add_argument("--no-anonymize", action="store_true",
                     help="Don't anonymize snippets (for debugging)")

    args = parser.parse_args()
    command = args.command or "export"

    # Store claude_dir for explicit passing to functions
    claude_dir = args.claude_dir
    if claude_dir:
        if not claude_dir.exists():
            print(f"Error: --claude-dir path does not exist: {claude_dir}")
            sys.exit(1)

    if command == "prep":
        prep(claude_dir=claude_dir)
        return

    if command == "status":
        status()
        return

    if command == "confirm":
        confirm(file_path=args.file)
        return

    if command == "update-skill":
        update_skill(args.target)
        return

    if command == "list":
        list_projects(claude_dir=claude_dir)
        return

    if command == "config":
        _handle_config(args)
        return

    if command == "index":
        _handle_index(args, claude_dir=claude_dir)
        return

    if command == "search":
        _handle_search(args)
        return

    _run_export(args)


def _parse_csv_arg(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _handle_config(args) -> None:
    """Handle the config subcommand."""
    has_changes = args.repo or args.exclude or args.redact or args.redact_usernames or args.confirm_projects
    if not has_changes:
        print(json.dumps(_mask_config_for_display(load_config()), indent=2))
        return
    configure(
        repo=args.repo,
        exclude=_parse_csv_arg(args.exclude),
        redact=_parse_csv_arg(args.redact),
        redact_usernames=_parse_csv_arg(args.redact_usernames),
        confirm_projects=args.confirm_projects or bool(args.exclude),
    )


def _handle_index(args, claude_dir: Path | None = None) -> None:
    """Handle the index subcommand - build search index."""
    projects = None
    if args.projects:
        projects = _parse_csv_arg(args.projects)
    
    print("=" * 50)
    print("  DataClaw — Building Search Index")
    print("=" * 50)
    
    # Use provided claude_dir or fall back to default
    effective_claude_dir = claude_dir or CLAUDE_DIR
    
    if not effective_claude_dir.exists():
        print(f"Error: {effective_claude_dir} not found. Is Claude Code installed?")
        sys.exit(1)
    
    result = build_index(projects=projects, force=args.force)
    
    if "error" in result and result["document_count"] == 0:
        print(f"Error: {result.get('error')}")
        if "available_projects" in result:
            print(f"Available projects: {', '.join(result['available_projects'])}")
        sys.exit(1)
    
    print(f"\nIndexed {result['document_count']} sessions from {len(result['projects_indexed'])} projects")
    print(f"Index stored at: {result.get('index_path', SEARCH_DB_PATH)}")
    
    if result.get("errors"):
        print(f"\nWarnings:")
        for err in result["errors"]:
            print(f"  - {err}")
    
    print(json.dumps({
        "stage": "indexed",
        "document_count": result["document_count"],
        "projects_indexed": result["projects_indexed"],
        "index_path": result.get("index_path"),
        "next_steps": [
            "Search your conversations: dataclaw search \"your query\"",
            "Re-index after new sessions: dataclaw index",
        ],
        "next_command": None,
    }, indent=2))


def _handle_search(args) -> None:
    """Handle the search subcommand - search indexed sessions."""
    # Determine anonymization setting
    anonymize = not args.no_anonymize
    
    if not args.json:
        print("=" * 50)
        print(f"  Searching: {args.query}")
        print("=" * 50)
    
    results = do_search(
        args.query, 
        limit=args.limit, 
        min_confidence=args.min_confidence,
        anonymize=anonymize,
    )
    
    if not results:
        if not args.json:
            print("No results found. Try:")
            print("  - Building the index: dataclaw index")
            print("  - Using different search terms")
            print("  - Lowering min-confidence: dataclaw search \"query\" --min-confidence 10")
        print(json.dumps({"results": [], "count": 0}, indent=2))
        return
    
    if args.json:
        # JSON-only output
        print(json.dumps({
            "query": args.query,
            "results": results,
            "count": len(results),
        }, indent=2))
    else:
        # Table output
        print(f"\nFound {len(results)} results:\n")
        print(f"{'Session ID':<30} {'Project':<20} {'Confidence':>10} {'Preview'}")
        print("-" * 100)
        
        for r in results:
            preview = r.get("snippet", "")[:50].replace("\n", " ")
            print(f"{r['id'][:28]:<30} {r['project'][:18]:<20} {r['confidence']:>8}% {preview}...")
        
        # Also output JSON for programmatic use
        print("\n---DATACLAW_JSON---")
        print(json.dumps({
            "query": args.query,
            "results": results,
            "count": len(results),
        }, indent=2))


def _run_export(args) -> None:
    """Run the export flow — discover, anonymize, export, optionally push."""
    # Gate: require `dataclaw confirm` before pushing
    if not args.no_push:
        config = load_config()
        if config.get("stage") != "confirmed":
            print(json.dumps({
                "error": "You must run `dataclaw confirm` before pushing.",
                "hint": "Export first with --no-push, review the data, then run `dataclaw confirm`.",
                "next_command": "dataclaw confirm",
            }, indent=2))
            sys.exit(1)

    print("=" * 50)
    print("  DataClaw — Claude Code Log Exporter")
    print("=" * 50)

    if not CLAUDE_DIR.exists():
        print(f"Error: {CLAUDE_DIR} not found. Is Claude Code installed?", file=sys.stderr)
        sys.exit(1)

    projects = discover_projects()
    if not projects:
        print("No Claude Code sessions found.", file=sys.stderr)
        sys.exit(1)

    config = load_config()

    total_sessions = sum(p["session_count"] for p in projects)
    total_size = sum(p["total_size_bytes"] for p in projects)
    print(f"\nFound {total_sessions} sessions across {len(projects)} projects "
          f"({_format_size(total_size)} raw)")

    # Resolve repo — CLI flag > config > auto-detect from HF username
    repo_id = args.repo or config.get("repo")
    if not repo_id and not args.no_push:
        hf_user = get_hf_username()
        if hf_user:
            repo_id = default_repo_name(hf_user)
            print(f"\nAuto-detected HF repo: {repo_id}")
            config["repo"] = repo_id
            save_config(config)

    # Apply exclusions
    excluded = set(config.get("excluded_projects", []))
    if args.all_projects:
        excluded = set()

    included = [p for p in projects if p["display_name"] not in excluded]
    excluded_projects = [p for p in projects if p["display_name"] in excluded]

    if excluded_projects:
        print(f"\nIncluding {len(included)} projects (excluding {len(excluded_projects)}):")
    else:
        print(f"\nIncluding all {len(included)} projects:")
    for p in included:
        print(f"  + {p['display_name']} ({p['session_count']} sessions)")
    for p in excluded_projects:
        print(f"  - {p['display_name']} (excluded)")

    if not included:
        print("\nNo projects to export. Run: dataclaw config --exclude ''")
        sys.exit(1)

    # Build anonymizer with extra usernames from config
    extra_usernames = config.get("redact_usernames", [])
    anonymizer = AnonymizerWrapper(extra_usernames=extra_usernames)

    # Custom strings to redact
    custom_strings = config.get("redact_strings", [])

    if extra_usernames:
        print(f"\nAnonymizing usernames: {', '.join(extra_usernames)}")
    if custom_strings:
        print(f"Redacting custom strings: {len(custom_strings)} configured")

    # Export
    output_path = args.output or Path("dataclaw_conversations.jsonl")

    print(f"\nExporting to {output_path}...")
    meta = export_to_jsonl(
        included, output_path, anonymizer, not args.no_thinking,
        custom_strings=custom_strings,
    )
    file_size = output_path.stat().st_size
    print(f"\nExported {meta['sessions']} sessions ({_format_size(file_size)})")
    if meta.get("skipped"):
        print(f"Skipped {meta['skipped']} abandoned/error sessions")
    if meta.get("redactions"):
        print(f"Redacted {meta['redactions']} secrets (API keys, tokens, emails, etc.)")
    print(f"Models: {', '.join(f'{m} ({c})' for m, c in sorted(meta['models'].items(), key=lambda x: -x[1]))}")

    _print_pii_guidance(output_path)

    config["last_export"] = {
        "timestamp": meta["exported_at"],
        "sessions": meta["sessions"],
        "models": meta["models"],
    }
    if args.no_push:
        config["stage"] = "review"
    save_config(config)

    if args.no_push:
        print(f"\nDone! JSONL file: {output_path}")
        abs_path = str(output_path.resolve())
        next_steps, next_command = _build_status_next_steps("review", config, None, None)
        json_block = {
            "stage": "review",
            "stage_number": 3,
            "total_stages": 4,
            "sessions": meta["sessions"],
            "output_file": abs_path,
            "pii_commands": _build_pii_commands(output_path),
            "next_steps": next_steps,
            "next_command": next_command,
        }
        print("\n---DATACLAW_JSON---")
        print(json.dumps(json_block, indent=2))
        return

    if not repo_id:
        print(f"\nNo HF repo. Log in first: huggingface-cli login")
        print(f"Then re-run dataclaw and it will auto-detect your username.")
        print(f"Or set manually: dataclaw config --repo username/my-personal-claude-code-data")
        print(f"\nLocal file: {output_path}")
        return

    push_to_huggingface(output_path, repo_id, meta)

    config["stage"] = "done"
    save_config(config)

    json_block = {
        "stage": "done",
        "stage_number": 4,
        "total_stages": 4,
        "dataset_url": f"https://huggingface.co/datasets/{repo_id}",
        "next_steps": [
            "Done! Dataset is live. To update later: dataclaw export",
            "To reconfigure: dataclaw prep then dataclaw config",
        ],
        "next_command": None,
    }
    print("\n---DATACLAW_JSON---")
    print(json.dumps(json_block, indent=2))


def _build_pii_commands(output_path: Path) -> list[str]:
    """Return grep commands for PII scanning."""
    p = str(output_path.resolve())
    return [
        f"grep -oE '[a-zA-Z0-9.+-]+@[a-zA-Z0-9.-]+\\.[a-z]{{2,}}' {p} | grep -v noreply | head -20",
        f"grep -oE 'eyJ[A-Za-z0-9_-]{{20,}}' {p} | head -5",
        f"grep -oE '(ghp_|sk-|hf_)[A-Za-z0-9_-]{{10,}}' {p} | head -5",
        f"grep -oE '[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}' {p} | sort -u",
    ]


def _print_pii_guidance(output_path: Path) -> None:
    """Print PII review guidance with concrete grep commands."""
    abs_output = output_path.resolve()
    print(f"\n{'=' * 50}")
    print("  IMPORTANT: Review your data before publishing!")
    print(f"{'=' * 50}")
    print("DataClaw's automatic redaction is NOT foolproof.")
    print("You should scan the exported data for remaining PII.")
    print()
    print("Quick checks (run these and review any matches):")
    print(f"  grep -i 'your_name' {abs_output}")
    print(f"  grep -oE '[a-zA-Z0-9.+-]+@[a-zA-Z0-9.-]+\\.[a-z]{{2,}}' {abs_output} | grep -v noreply | head -20")
    print(f"  grep -oE 'eyJ[A-Za-z0-9_-]{{20,}}' {abs_output} | head -5")
    print(f"  grep -oE '(ghp_|sk-|hf_)[A-Za-z0-9_-]{{10,}}' {abs_output} | head -5")
    print(f"  grep -oE '[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}' {abs_output} | sort -u")
    print()
    print("NEXT: Ask the user for their full name, then scan for it:")
    print(f"  grep -i 'THEIR_NAME' {abs_output} | head -10")
    print()
    print("To add custom redactions, then re-export:")
    print("  dataclaw config --redact-usernames 'github_handle,discord_name'")
    print("  dataclaw config --redact 'secret-domain.com,my-api-key'")
    print(f"  dataclaw export --no-push -o {abs_output}")
    print()
    print(f"Found an issue? Help improve DataClaw: {REPO_URL}/issues")


if __name__ == "__main__":
    main()
