"""CLI for DataClaw — export coding agent conversations to Hugging Face."""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, cast

from .anonymizer import Anonymizer
from .config import CONFIG_FILE, DataClawConfig, load_config, save_config
from .parser import CLAUDE_DIR, CODEX_DIR, CUSTOM_DIR, GEMINI_DIR, KIMI_DIR, OPENCODE_DIR, OPENCLAW_DIR, discover_projects, parse_project_sessions
from .secrets import _has_mixed_char_types, _shannon_entropy, redact_session

HF_TAG = "dataclaw"
REPO_URL = "https://github.com/kaiaiagent/dataclaw"
SKILL_URL = "https://raw.githubusercontent.com/kaiaiagent/dataclaw/main/docs/SKILL.md"

REQUIRED_REVIEW_ATTESTATIONS: dict[str, str] = {
    "asked_full_name": "I asked the user for their full name and scanned for it.",
    "asked_sensitive_entities": "I asked about company/client/internal names and private URLs.",
    "manual_scan_done": "I performed a manual sample scan of exported sessions.",
}
MIN_ATTESTATION_CHARS = 24
MIN_MANUAL_SCAN_SESSIONS = 20

CONFIRM_COMMAND_EXAMPLE = (
    "dataclaw confirm "
    "--full-name \"THEIR FULL NAME\" "
    "--attest-full-name \"Asked for full name and scanned export for THEIR FULL NAME.\" "
    "--attest-sensitive \"Asked about company/client/internal names and private URLs; user response recorded and redactions updated if needed.\" "
    "--attest-manual-scan \"Manually scanned 20 sessions across beginning/middle/end and reviewed findings with the user.\""
)

CONFIRM_COMMAND_SKIP_FULL_NAME_EXAMPLE = (
    "dataclaw confirm "
    "--skip-full-name-scan "
    "--attest-full-name \"User declined to share full name; skipped exact-name scan.\" "
    "--attest-sensitive \"Asked about company/client/internal names and private URLs; user response recorded and redactions updated if needed.\" "
    "--attest-manual-scan \"Manually scanned 20 sessions across beginning/middle/end and reviewed findings with the user.\""
)

EXPORT_REVIEW_PUBLISH_STEPS = [
    "Step 1/3: Export locally only: dataclaw export --no-push --output /tmp/dataclaw_export.jsonl",
    "Step 2/3: Review/redact, then run confirm: dataclaw confirm ...",
    "Step 3/3: After explicit user approval, publish: dataclaw export --publish-attestation \"User explicitly approved publishing to Hugging Face.\"",
]

SETUP_TO_PUBLISH_STEPS = [
    "Step 1/6: Run prep/list to review project scope: dataclaw prep && dataclaw list",
    "Step 2/6: Explicitly choose source scope: dataclaw config --source <claude|codex|gemini|all>",
    "Step 3/6: Configure exclusions/redactions and confirm projects: dataclaw config ...",
    "Step 4/6: Export locally only: dataclaw export --no-push --output /tmp/dataclaw_export.jsonl",
    "Step 5/6: Review and confirm: dataclaw confirm ...",
    "Step 6/6: After explicit user approval, publish: dataclaw export --publish-attestation \"User explicitly approved publishing to Hugging Face.\"",
]

EXPLICIT_SOURCE_CHOICES = {"claude", "codex", "custom", "gemini", "kimi", "opencode", "openclaw", "all", "both"}
SOURCE_CHOICES = ["auto", "claude", "codex", "custom", "gemini", "kimi", "opencode", "openclaw", "all"]


def _mask_secret(s: str) -> str:
    """Mask a secret string for display, e.g. 'hf_OOgd...oEVH'."""
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}...{s[-4:]}"


def _mask_config_for_display(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of config with redact_strings values masked."""
    out = dict(config)
    if out.get("redact_strings"):
        out["redact_strings"] = [_mask_secret(s) for s in out["redact_strings"]]
    return out


def _source_label(source_filter: str) -> str:
    source_filter = _normalize_source_filter(source_filter)
    if source_filter == "claude":
        return "Claude Code"
    if source_filter == "codex":
        return "Codex"
    if source_filter == "gemini":
        return "Gemini CLI"
    if source_filter == "opencode":
        return "OpenCode"
    if source_filter == "openclaw":
        return "OpenClaw"
    if source_filter == "kimi":
        return "Kimi CLI"
    if source_filter == "custom":
        return "Custom"
    return "Claude Code, Codex, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, or Custom"


def _normalize_source_filter(source_filter: str) -> str:
    if source_filter in ("all", "both"):
        return "auto"
    return source_filter


def _is_explicit_source_choice(source_filter: str | None) -> bool:
    return source_filter in EXPLICIT_SOURCE_CHOICES


def _resolve_source_choice(
    requested_source: str,
    config: DataClawConfig | None = None,
) -> tuple[str, bool]:
    """Resolve source choice from CLI + config.

    Returns:
      (source_choice, explicit) where source_choice is one of
      "claude" | "codex" | "gemini" | "opencode" | "openclaw" | "all" | "auto".
    """
    if _is_explicit_source_choice(requested_source):
        return requested_source, True
    if config:
        configured_source = config.get("source")
        if _is_explicit_source_choice(configured_source):
            return str(configured_source), True
    return "auto", False


def _has_session_sources(source_filter: str = "auto") -> bool:
    source_filter = _normalize_source_filter(source_filter)
    if source_filter == "claude":
        return CLAUDE_DIR.exists()
    if source_filter == "codex":
        return CODEX_DIR.exists()
    if source_filter == "gemini":
        return GEMINI_DIR.exists()
    if source_filter == "opencode":
        return OPENCODE_DIR.exists()
    if source_filter == "openclaw":
        return OPENCLAW_DIR.exists()
    if source_filter == "kimi":
        return KIMI_DIR.exists()
    if source_filter == "custom":
        return CUSTOM_DIR.exists()
    return CLAUDE_DIR.exists() or CODEX_DIR.exists() or CUSTOM_DIR.exists() or GEMINI_DIR.exists() or KIMI_DIR.exists() or OPENCODE_DIR.exists() or OPENCLAW_DIR.exists()


def _filter_projects_by_source(projects: list[dict], source_filter: str) -> list[dict]:
    source_filter = _normalize_source_filter(source_filter)
    if source_filter == "auto":
        return projects
    return [p for p in projects if p.get("source", "claude") == source_filter]


def _format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


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
    """Standard repo name: {username}/my-personal-codex-data"""
    return f"{hf_username}/my-personal-codex-data"


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
        configured_source = config.get("source")
        source_confirmed = _is_explicit_source_choice(configured_source)
        list_command = (
            f"dataclaw list --source {configured_source}" if source_confirmed else "dataclaw list"
        )
        steps = []
        if not source_confirmed:
            steps.append(
                "Ask the user to explicitly choose export source scope: Claude Code, Codex, Gemini, or all. "
                "Then set it: dataclaw config --source <claude|codex|gemini|all>. "
                "Do not run export until source scope is explicitly confirmed."
            )
        else:
            steps.append(
                f"Source scope is currently set to '{configured_source}'. "
                "If the user wants a different scope, run: dataclaw config --source <claude|codex|gemini|all>."
            )
        if not projects_confirmed:
            steps.append(
                f"Run: {list_command} — then send the FULL project/folder list to the user in your next message "
                "(name, source, sessions, size, excluded), and ask which to EXCLUDE."
            )
            steps.append(
                "Configure project scope: dataclaw config --exclude \"project1,project2\" "
                "or dataclaw config --confirm-projects (to include all listed projects). "
                "Do not run export until this folder review is confirmed."
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
                "Ask the user for their full name to run an exact-name privacy check against the export. If they decline, you may skip this check with --skip-full-name-scan and include a clear attestation.",
                "Run PII scan commands and review results with the user.",
                "Ask the user: 'Are there any company names, internal project names, client names, private URLs, or other people's names in your conversations that you'd want redacted? Any custom domains or internal tools?' Add anything they mention with dataclaw config --redact.",
                "Do a deep manual scan: sample ~20 sessions from the export (beginning, middle, end) and scan for names, private URLs, company names, credentials in conversation text, and anything else that looks sensitive. Report findings to the user.",
                "If PII found in any of the above, add redactions (dataclaw config --redact) and re-export: dataclaw export --no-push",
                (
                    "Run: "
                    + CONFIRM_COMMAND_EXAMPLE
                    + " — scans for PII, shows project breakdown, and unlocks pushing."
                ),
                "Do NOT push until the user explicitly confirms. Once confirmed, push: dataclaw export --publish-attestation \"User explicitly approved publishing to Hugging Face.\"",
            ],
            "dataclaw confirm",
        )

    if stage == "confirmed":
        return (
            [
                "User has reviewed the export. Ask: 'Ready to publish to Hugging Face?' and push: dataclaw export --publish-attestation \"User explicitly approved publishing to Hugging Face.\"",
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


def list_projects(source_filter: str = "auto") -> None:
    """Print all projects as JSON (for agents to parse)."""
    projects = _filter_projects_by_source(discover_projects(), source_filter)
    if not projects:
        print(f"No {_source_label(source_filter)} sessions found.")
        return
    config = load_config()
    excluded = set(config.get("excluded_projects", []))
    print(json.dumps(
        [{"name": p["display_name"], "sessions": p["session_count"],
          "size": _format_size(p["total_size_bytes"]),
          "excluded": p["display_name"] in excluded,
          "source": p.get("source", "claude")}
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
    source: str | None = None,
    exclude: list[str] | None = None,
    redact: list[str] | None = None,
    redact_usernames: list[str] | None = None,
    confirm_projects: bool = False,
):
    """Set config values non-interactively. Lists are MERGED (append), not replaced."""
    config = load_config()
    if repo is not None:
        config["repo"] = repo
    if source is not None:
        config["source"] = source
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
    anonymizer: Anonymizer,
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
                source=project.get("source", "claude"),
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
  - codex-cli
  - gemini-cli
  - opencode
  - openclaw
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


SKILL_TARGETS: dict[str, dict[str, str]] = {
    "claude": {
        "dest_template": ".claude/skills/dataclaw/SKILL.md",
        "source_file": "SKILL.md",
        "source_url": SKILL_URL,
    },
    "openclaw": {
        "dest_template": "DATACLAW_AGENTS.md",
        "source_file": "SKILL.md",
        "source_url": SKILL_URL,
    },
    "codex": {
        "dest_template": "DATACLAW_AGENTS.md",
        "source_file": "SKILL.md",
        "source_url": SKILL_URL,
    },
    "cline": {
        "dest_template": ".cline/dataclaw/SKILL.md",
        "source_file": "SKILL.md",
        "source_url": SKILL_URL,
    },
}


def update_skill(target: str) -> None:
    """Download and install the dataclaw skill for a coding agent."""
    target_config = SKILL_TARGETS.get(target)
    if not target_config:
        print(f"Error: unknown target '{target}'. Supported: {', '.join(SKILL_TARGETS)}", file=sys.stderr)
        sys.exit(1)

    dest = Path.cwd() / target_config["dest_template"]
    dest.parent.mkdir(parents=True, exist_ok=True)

    url = target_config["source_url"]
    print(f"Downloading skill from {url}...")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            content = resp.read().decode()
    except (OSError, urllib.error.URLError) as e:
        print(f"Error downloading skill: {e}", file=sys.stderr)
        # Fall back to bundled copy
        bundled = Path(__file__).resolve().parent.parent / "docs" / target_config["source_file"]
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
        "target": target,
        "next_steps": [
            "Run: dataclaw scan",
            "Then: dataclaw inbox --json",
            "Or open the full UI: dataclaw serve",
        ],
        "next_command": "dataclaw scan",
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
        "source": config.get("source"),
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
        "error": "No export file found.",
        "hint": "Run step 1 first to generate a local export file.",
        "blocked_on_step": "Step 1/3",
        "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
        "next_command": "dataclaw export --no-push --output /tmp/dataclaw_export.jsonl",
    }, indent=2))
    sys.exit(1)


def _scan_high_entropy_strings(content: str, max_results: int = 15) -> list[dict]:
    """Scan for high-entropy random strings that might be leaked secrets.

    Complements the regex-based _scan_pii by catching unquoted tokens
    that slipped through Layer 1 (secrets.py) redaction.
    """
    if not content:
        return []

    _CANDIDATE_RE = re.compile(r'[A-Za-z0-9_/+=.-]{20,}')

    # Prefixes already caught by other scans
    _KNOWN_PREFIXES = ("eyJ", "ghp_", "gho_", "ghs_", "ghr_", "sk-", "hf_",
                       "AKIA", "pypi-", "npm_", "xox")

    # Benign prefixes that look random but aren't secrets
    _BENIGN_PREFIXES = ("https://", "http://", "sha256-", "sha384-", "sha512-",
                        "sha1-", "data:", "file://", "mailto:")

    # Substrings that indicate non-secret content
    _BENIGN_SUBSTRINGS = ("node_modules", "[REDACTED]", "package-lock",
                          "webpack", "babel", "eslint", ".chunk.",
                          "vendor/", "dist/", "build/")

    # File extensions that indicate path-like strings
    _FILE_EXTENSIONS = (".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html",
                        ".json", ".yaml", ".yml", ".toml", ".md", ".rst",
                        ".txt", ".sh", ".go", ".rs", ".java", ".rb", ".php",
                        ".c", ".h", ".cpp", ".hpp", ".swift", ".kt",
                        ".lock", ".cfg", ".ini", ".xml", ".svg", ".png",
                        ".jpg", ".gif", ".woff", ".ttf", ".map", ".vue",
                        ".scss", ".less", ".sql", ".env", ".log")

    _HEX_RE = re.compile(r'^[0-9a-fA-F]+$')
    _UUID_RE = re.compile(
        r'^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$'
    )

    # Collect unique candidates first
    unique_candidates: dict[str, list[int]] = {}
    for m in _CANDIDATE_RE.finditer(content):
        token = m.group(0)
        if token not in unique_candidates:
            unique_candidates[token] = []
        unique_candidates[token].append(m.start())

    results = []
    for token, positions in unique_candidates.items():
        # --- cheap filters first ---

        # Skip known prefixes (already caught by other scans)
        if any(token.startswith(p) for p in _KNOWN_PREFIXES):
            continue

        # Skip hex-only strings (git hashes etc.)
        if _HEX_RE.match(token):
            continue

        # Skip UUIDs (with or without hyphens)
        if _UUID_RE.match(token):
            continue

        # Skip strings containing file extensions
        token_lower = token.lower()
        if any(ext in token_lower for ext in _FILE_EXTENSIONS):
            continue

        # Skip path-like strings (2+ slashes)
        if token.count("/") >= 2:
            continue

        # Skip 3+ dots (domain names, version strings)
        if token.count(".") >= 3:
            continue

        # Skip benign prefixes
        if any(token_lower.startswith(p) for p in _BENIGN_PREFIXES):
            continue

        # Skip benign substrings
        if any(sub in token_lower for sub in _BENIGN_SUBSTRINGS):
            continue

        # Require mixed char types (upper + lower + digit)
        if not _has_mixed_char_types(token):
            continue

        # --- entropy check (most expensive, done last) ---
        entropy = _shannon_entropy(token)
        if entropy < 4.0:
            continue

        # Build context from first occurrence
        pos = positions[0]
        ctx_start = max(0, pos - 40)
        ctx_end = min(len(content), pos + len(token) + 40)
        context = content[ctx_start:ctx_end].replace("\n", " ")

        results.append({
            "match": token,
            "entropy": round(entropy, 2),
            "context": context,
        })

    # Sort by entropy descending, cap at max_results
    results.sort(key=lambda r: r["entropy"], reverse=True)
    return results[:max_results]


def _scan_pii(file_path: Path) -> dict:
    """Run PII regex scans on the export file. Returns dict of findings."""
    import re

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

    high_entropy = _scan_high_entropy_strings(content)
    if high_entropy:
        results["high_entropy_strings"] = high_entropy

    return results


def _normalize_attestation_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split()).strip()
    return " ".join(str(value).split()).strip()


def _extract_manual_scan_sessions(attestation: str) -> int | None:
    numbers = [int(n) for n in re.findall(r"\b(\d+)\b", attestation)]
    return max(numbers) if numbers else None


def _scan_for_text_occurrences(
    file_path: Path, query: str, max_examples: int = 5,
) -> dict[str, object]:
    """Scan file for case-insensitive occurrences of query and return a compact summary."""
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    matches = 0
    examples: list[dict[str, object]] = []
    try:
        with open(file_path, errors="replace") as f:
            for line_no, line in enumerate(f, start=1):
                if pattern.search(line):
                    matches += 1
                    if len(examples) < max_examples:
                        excerpt = line.strip()
                        if len(excerpt) > 220:
                            excerpt = f"{excerpt[:220]}..."
                        examples.append({"line": line_no, "excerpt": excerpt})
    except OSError as e:
        return {
            "query": query,
            "match_count": 0,
            "examples": [],
            "error": str(e),
        }
    return {
        "query": query,
        "match_count": matches,
        "examples": examples,
    }


def _collect_review_attestations(
    attest_asked_full_name: object,
    attest_asked_sensitive: object,
    attest_manual_scan: object,
    full_name: str | None,
    skip_full_name_scan: bool = False,
) -> tuple[dict[str, str], dict[str, str], int | None]:
    provided = {
        "asked_full_name": _normalize_attestation_text(attest_asked_full_name),
        "asked_sensitive_entities": _normalize_attestation_text(attest_asked_sensitive),
        "manual_scan_done": _normalize_attestation_text(attest_manual_scan),
    }
    errors: dict[str, str] = {}

    full_name_attestation = provided["asked_full_name"]
    if len(full_name_attestation) < MIN_ATTESTATION_CHARS:
        errors["asked_full_name"] = "Provide a detailed text attestation for full-name review."
    else:
        lower = full_name_attestation.lower()
        if skip_full_name_scan:
            mentions_skip = any(
                token in lower
                for token in ("skip", "skipped", "declined", "opt out", "prefer not")
            )
            if "full name" not in lower or not mentions_skip:
                errors["asked_full_name"] = (
                    "When skipping full-name scan, attestation must say the user declined/skipped full name."
                )
        else:
            full_name_lower = (full_name or "").lower()
            full_name_tokens = [t for t in re.split(r"\s+", full_name_lower) if len(t) > 1]
            if "ask" not in lower or "scan" not in lower:
                errors["asked_full_name"] = (
                    "Full-name attestation must mention that you asked the user and scanned the export."
                )
            elif full_name_tokens and not all(token in lower for token in full_name_tokens):
                errors["asked_full_name"] = (
                    "Full-name attestation must reference the same full name passed in --full-name."
                )

    sensitive_attestation = provided["asked_sensitive_entities"]
    if len(sensitive_attestation) < MIN_ATTESTATION_CHARS:
        errors["asked_sensitive_entities"] = (
            "Provide a detailed text attestation for sensitive-entity review."
        )
    else:
        lower = sensitive_attestation.lower()
        asked = "ask" in lower
        topics = any(
            token in lower
            for token in ("company", "client", "internal", "url", "domain", "tool", "name")
        )
        outcome = any(
            token in lower
            for token in ("none", "no", "redact", "added", "updated", "configured")
        )
        if not asked or not topics or not outcome:
            errors["asked_sensitive_entities"] = (
                "Sensitive attestation must say what you asked and the outcome "
                "(none found or redactions updated)."
            )

    manual_attestation = provided["manual_scan_done"]
    manual_sessions = _extract_manual_scan_sessions(manual_attestation)
    if len(manual_attestation) < MIN_ATTESTATION_CHARS:
        errors["manual_scan_done"] = "Provide a detailed text attestation for the manual scan."
    else:
        lower = manual_attestation.lower()
        if "manual" not in lower or "scan" not in lower:
            errors["manual_scan_done"] = (
                "Manual scan attestation must explicitly mention a manual scan."
            )
        elif manual_sessions is None or manual_sessions < MIN_MANUAL_SCAN_SESSIONS:
            errors["manual_scan_done"] = (
                f"Manual scan attestation must include a reviewed-session count >= {MIN_MANUAL_SCAN_SESSIONS}."
            )

    return provided, errors, manual_sessions


def _validate_publish_attestation(attestation: object) -> tuple[str, str | None]:
    normalized = _normalize_attestation_text(attestation)
    if len(normalized) < MIN_ATTESTATION_CHARS:
        return normalized, "Provide a detailed text publish attestation."
    lower = normalized.lower()
    if "approv" not in lower or ("publish" not in lower and "push" not in lower):
        return normalized, (
            "Publish attestation must state that the user explicitly approved publishing/pushing."
        )
    return normalized, None


def confirm(
    file_path: Path | None = None,
    full_name: str | None = None,
    attest_asked_full_name: str | None = None,
    attest_asked_sensitive: str | None = None,
    attest_manual_scan: str | None = None,
    skip_full_name_scan: bool = False,
) -> None:
    """Scan export for PII, summarize projects, and unlock pushing. JSON output."""
    config = load_config()
    last_export = config.get("last_export", {})
    file_path = _find_export_file(file_path)

    normalized_full_name = _normalize_attestation_text(full_name)
    if skip_full_name_scan and normalized_full_name:
        print(json.dumps({
            "error": "Use either --full-name or --skip-full-name-scan, not both.",
            "hint": (
                "Provide --full-name for an exact-name scan, or use --skip-full-name-scan "
                "if the user declines sharing their name."
            ),
            "blocked_on_step": "Step 2/3",
            "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
            "next_command": CONFIRM_COMMAND_EXAMPLE,
        }, indent=2))
        sys.exit(1)
    if not normalized_full_name and not skip_full_name_scan:
        print(json.dumps({
            "error": "Missing required --full-name for verification scan.",
            "hint": (
                "Ask the user for their full name and pass it via --full-name "
                "to run an exact-name privacy check. If the user declines, rerun with "
                "--skip-full-name-scan and a full-name attestation describing the skip."
            ),
            "blocked_on_step": "Step 2/3",
            "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
            "next_command": CONFIRM_COMMAND_SKIP_FULL_NAME_EXAMPLE,
        }, indent=2))
        sys.exit(1)

    attestations, attestation_errors, manual_scan_sessions = _collect_review_attestations(
        attest_asked_full_name=attest_asked_full_name,
        attest_asked_sensitive=attest_asked_sensitive,
        attest_manual_scan=attest_manual_scan,
        full_name=normalized_full_name if normalized_full_name else None,
        skip_full_name_scan=skip_full_name_scan,
    )
    if attestation_errors:
        print(json.dumps({
            "error": "Missing or invalid review attestations.",
            "attestation_errors": attestation_errors,
            "required_attestations": REQUIRED_REVIEW_ATTESTATIONS,
            "blocked_on_step": "Step 2/3",
            "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
            "next_command": CONFIRM_COMMAND_EXAMPLE,
        }, indent=2))
        sys.exit(1)

    if skip_full_name_scan:
        full_name_scan = {
            "query": None,
            "match_count": 0,
            "examples": [],
            "skipped": True,
            "reason": "User declined sharing full name; exact-name scan skipped.",
        }
    else:
        full_name_scan = _scan_for_text_occurrences(file_path, normalized_full_name)

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
    config["review_attestations"] = attestations
    config["review_verification"] = {
        "full_name": normalized_full_name if not skip_full_name_scan else None,
        "full_name_scan_skipped": skip_full_name_scan,
        "full_name_matches": full_name_scan.get("match_count", 0),
        "manual_scan_sessions": manual_scan_sessions,
    }
    config["last_confirm"] = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "file": str(file_path.resolve()),
        "pii_findings": bool(pii_findings),
        "full_name": normalized_full_name if not skip_full_name_scan else None,
        "full_name_scan_skipped": skip_full_name_scan,
        "full_name_matches": full_name_scan.get("match_count", 0),
        "manual_scan_sessions": manual_scan_sessions,
    }
    save_config(config)

    next_steps = [
        "Show the user the project breakdown, full-name scan, and PII scan results above.",
    ]
    if full_name_scan.get("skipped"):
        next_steps.append(
            "Full-name scan was skipped at user request. Ensure this was explicitly reviewed with the user."
        )
    elif full_name_scan.get("match_count", 0):
        next_steps.append(
            "Full-name scan found matches. Review them with the user and redact if needed, then re-export with --no-push."
        )
    if pii_findings:
        next_steps.append(
            "PII findings detected — review each one with the user. "
            "If real: dataclaw config --redact \"string\" then re-export with --no-push. "
            "False positives can be ignored."
        )
    if "high_entropy_strings" in pii_findings:
        next_steps.append(
            "High-entropy strings detected — these may be leaked secrets (API keys, tokens, "
            "passwords) that escaped automatic redaction. Review each one using the provided "
            "context snippets. If any are real secrets, redact with: "
            "dataclaw config --redact \"the_secret\" then re-export with --no-push."
        )
    next_steps.extend([
        "If any project should be excluded, run: dataclaw config --exclude \"project_name\" and re-export with --no-push.",
        f"This will publish {total} sessions ({_format_size(file_size)}) publicly to Hugging Face"
        + (f" at {repo_id}" if repo_id else "") + ". Ask the user: 'Are you ready to proceed?'",
        "Once confirmed, push: dataclaw export --publish-attestation \"User explicitly approved publishing to Hugging Face.\"",
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
        "full_name_scan": full_name_scan,
        "manual_scan_sessions": manual_scan_sessions,
        "repo": repo_id,
        "last_export_timestamp": last_export.get("timestamp"),
        "next_steps": next_steps,
        "next_command": "dataclaw export --publish-attestation \"User explicitly approved publishing to Hugging Face.\"",
        "attestations": attestations,
    }
    print(json.dumps(result, indent=2))


def prep(source_filter: str = "auto") -> None:
    """Data prep — discover projects, detect HF auth, output JSON.

    Designed to be called by an agent which handles the interactive parts.
    Outputs pure JSON to stdout so agents can parse it directly.
    """
    config = load_config()
    resolved_source_choice, source_explicit = _resolve_source_choice(source_filter, config)
    effective_source_filter = _normalize_source_filter(resolved_source_choice)

    if not _has_session_sources(effective_source_filter):
        if effective_source_filter == "claude":
            err = "~/.claude was not found."
        elif effective_source_filter == "codex":
            err = "~/.codex was not found."
        elif effective_source_filter == "gemini":
            from .parser import GEMINI_DIR
            err = f"{GEMINI_DIR} was not found."
        else:
            err = "None of ~/.claude, ~/.codex, or ~/.gemini/tmp were found."
        print(json.dumps({"error": err}))
        sys.exit(1)

    projects = _filter_projects_by_source(discover_projects(), effective_source_filter)
    if not projects:
        print(json.dumps({"error": f"No {_source_label(effective_source_filter)} sessions found."}))
        sys.exit(1)

    excluded = set(config.get("excluded_projects", []))

    # Use _compute_stage to determine where we are
    stage, stage_number, hf_user = _compute_stage(config)

    repo_id = config.get("repo")
    if not repo_id and hf_user:
        repo_id = default_repo_name(hf_user)

    # Build contextual next_steps
    stage_config = cast(DataClawConfig, dict(config))
    if source_explicit:
        stage_config["source"] = resolved_source_choice
    next_steps, next_command = _build_status_next_steps(stage, stage_config, hf_user, repo_id)

    # Persist stage
    config["stage"] = stage
    save_config(config)

    result = {
        "stage": stage,
        "stage_number": stage_number,
        "total_stages": 4,
        "next_command": next_command,
        "requested_source_filter": source_filter,
        "source_filter": resolved_source_choice,
        "source_selection_confirmed": source_explicit,
        "hf_logged_in": hf_user is not None,
        "hf_username": hf_user,
        "repo": repo_id,
        "projects": [
            {
                "name": p["display_name"],
                "sessions": p["session_count"],
                "size": _format_size(p["total_size_bytes"]),
                "excluded": p["display_name"] in excluded,
                "source": p.get("source", "claude"),
            }
            for p in projects
        ],
        "redact_strings": [_mask_secret(s) for s in config.get("redact_strings", [])],
        "redact_usernames": config.get("redact_usernames", []),
        "config_file": str(CONFIG_FILE),
        "next_steps": next_steps,
    }
    print(json.dumps(result, indent=2))


def _run_scan(source_filter: str | None = None) -> None:
    """One-shot scan: index sessions into the workbench database."""
    from .daemon import Scanner
    from .index import get_stats, open_index

    scanner = Scanner(source_filter=source_filter)
    print("Scanning sessions...")
    results = scanner.scan_once()

    total_new = sum(results.values())
    if total_new:
        print(f"Indexed {total_new} new sessions:")
        for source, count in sorted(results.items()):
            if count > 0:
                print(f"  {source}: {count}")
    else:
        print("No new sessions found.")

    conn = open_index()
    stats = get_stats(conn)
    conn.close()

    print(f"\nTotal indexed: {stats['total']}")
    if stats["by_status"]:
        for status, count in sorted(stats["by_status"].items()):
            print(f"  {status}: {count}")
    if stats["by_source"]:
        print("By source:")
        for source, count in sorted(stats["by_source"].items()):
            print(f"  {source}: {count}")


def _run_inbox(
    status: str | None = None,
    source: str | None = None,
    limit: int = 20,
    output_json: bool = False,
) -> None:
    """Show indexed sessions in the terminal."""
    from .index import get_stats, open_index, query_sessions

    conn = open_index()
    sessions = query_sessions(conn, status=status, source=source, limit=limit)
    stats = get_stats(conn)
    conn.close()

    if output_json:
        # Parse JSON fields for clean output
        items = []
        for i, s in enumerate(sessions, 1):
            value_badges = s.get("value_badges", [])
            if isinstance(value_badges, str):
                try:
                    value_badges = json.loads(value_badges)
                except (json.JSONDecodeError, ValueError):
                    value_badges = []
            risk_badges = s.get("risk_badges", [])
            if isinstance(risk_badges, str):
                try:
                    risk_badges = json.loads(risk_badges)
                except (json.JSONDecodeError, ValueError):
                    risk_badges = []
            items.append({
                "index": i,
                "session_id": s.get("session_id"),
                "display_title": s.get("display_title", ""),
                "source": s.get("source", ""),
                "model": s.get("model"),
                "messages": s.get("user_messages", 0) + s.get("assistant_messages", 0),
                "tokens": s.get("input_tokens", 0) + s.get("output_tokens", 0),
                "outcome_badge": s.get("outcome_badge"),
                "value_badges": value_badges,
                "risk_badges": risk_badges,
                "review_status": s.get("review_status", "new"),
                "project": s.get("project", ""),
                "task_type": s.get("task_type"),
                "start_time": s.get("start_time"),
            })
        print(json.dumps({
            "sessions": items,
            "total": stats["total"],
            "showing": len(items),
            "by_status": stats.get("by_status", {}),
        }, indent=2))
        return

    if not sessions:
        print("No sessions found. Run `dataclaw scan` first.")
        return

    # Print a compact table
    print(f"{'Status':<12} {'Source':<10} {'Model':<25} {'Msgs':>5} {'Tokens':>8}  Title")
    print("-" * 100)
    for s in sessions:
        title = (s.get("display_title") or "")[:45]
        model = (s.get("model") or "")[:24]
        msgs = s.get("user_messages", 0) + s.get("assistant_messages", 0)
        tokens = s.get("input_tokens", 0) + s.get("output_tokens", 0)
        status_str = s.get("review_status", "new")
        source_str = s.get("source", "")
        # Badges
        badges = []
        outcome = s.get("outcome_badge", "")
        if outcome and outcome != "unknown":
            badges.append(outcome)
        try:
            value_badges = json.loads(s.get("value_badges", "[]")) if isinstance(s.get("value_badges"), str) else (s.get("value_badges") or [])
        except (json.JSONDecodeError, ValueError):
            value_badges = []
        try:
            risk_badges = json.loads(s.get("risk_badges", "[]")) if isinstance(s.get("risk_badges"), str) else (s.get("risk_badges") or [])
        except (json.JSONDecodeError, ValueError):
            risk_badges = []
        badges.extend(value_badges[:2])
        badges.extend(risk_badges[:2])
        badge_str = f" [{', '.join(badges)}]" if badges else ""

        print(f"{status_str:<12} {source_str:<10} {model:<25} {msgs:>5} {tokens:>8}  {title}{badge_str}")

    print(f"\n{len(sessions)} sessions shown. Use `dataclaw serve` for the full review UI.")


def _run_review_action(
    action: str,
    session_ids: list[str],
    reason: str | None = None,
) -> None:
    """Update session review status for one or more sessions."""
    from .index import open_index, update_session

    if not session_ids:
        print(json.dumps({"error": "No session IDs provided."}))
        sys.exit(1)

    conn = open_index()
    results = []
    for sid in session_ids:
        ok = update_session(conn, sid, status=action, reason=reason)
        results.append({"session_id": sid, "ok": ok})
    conn.close()

    success = sum(1 for r in results if r["ok"])
    print(json.dumps({
        "action": action,
        "updated": success,
        "not_found": len(results) - success,
        "results": results,
    }, indent=2))


def _truncate(text: str, max_len: int = 80) -> str:
    """Truncate text to max_len, appending '...' if shortened."""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def _format_duration(seconds: int | None) -> str:
    """Format duration in seconds to a human-readable string like '12m' or '1h 5m'."""
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining = minutes % 60
    if remaining:
        return f"{hours}h {remaining}m"
    return f"{hours}h"


def _format_tokens(count: int) -> str:
    """Format token count to compact form like '15.2k'."""
    if count < 1000:
        return str(count)
    if count < 10000:
        return f"{count / 1000:.1f}k"
    return f"{count // 1000}k"


def _get_message_text(msg: dict) -> str:
    """Extract text content from a message dict."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                return block
            if isinstance(block, dict) and block.get("text"):
                return block["text"]
    return ""


def _extract_tool_uses(msg: dict) -> list[dict]:
    """Extract tool uses from a message, handling both parsed and raw formats."""
    tool_uses = msg.get("tool_uses", [])
    if tool_uses:
        return tool_uses
    # Check content blocks for tool use
    content = msg.get("content")
    if isinstance(content, list):
        uses = []
        for block in content:
            if isinstance(block, dict) and block.get("tool"):
                inp = block.get("input", {})
                first_arg = ""
                if isinstance(inp, dict):
                    for v in inp.values():
                        if isinstance(v, str) and v.strip():
                            first_arg = v.strip()
                            break
                uses.append({
                    "tool": block["tool"],
                    "input": inp,
                    "output": block.get("output", ""),
                    "status": block.get("status", ""),
                    "first_arg": first_arg,
                })
        return uses
    return []


def _run_score_view(args) -> None:
    """Show condensed session view for AI scoring."""
    from .index import get_session_detail, open_index, query_sessions

    conn = open_index()

    if args.batch:
        # Batch mode: load multiple sessions in compact format
        sessions = query_sessions(
            conn,
            source=args.source,
            limit=args.limit,
            offset=args.offset,
        )
        if not sessions:
            print("No sessions found.")
            conn.close()
            return

        total = len(sessions)
        for idx, s in enumerate(sessions, 1):
            sid = s["session_id"]
            detail = get_session_detail(conn, sid)
            if not detail:
                continue

            source = detail.get("source", "?")
            model = detail.get("model", "?")
            project = detail.get("project", "?")
            duration = _format_duration(detail.get("duration_seconds"))
            total_tokens = _format_tokens(
                (detail.get("input_tokens") or 0) + (detail.get("output_tokens") or 0)
            )
            task_type = detail.get("task_type", "unknown")
            outcome = detail.get("outcome_badge", "unknown")
            user_msgs = detail.get("user_messages", 0)
            asst_msgs = detail.get("assistant_messages", 0)

            # First user message
            first_msg = ""
            messages = detail.get("messages", [])
            for msg in messages:
                if msg.get("role") == "user":
                    first_msg = _get_message_text(msg)
                    break

            # Condensed flow
            flow_parts = []
            for msg in messages:
                role = msg.get("role", "")
                text = _get_message_text(msg)
                summary = _truncate(text, 30) if text else ""
                tool_uses = _extract_tool_uses(msg)
                tool_names = [t.get("tool", "") for t in tool_uses if t.get("tool")]
                if role == "user":
                    label = f"User→{summary}" if summary else "User"
                elif role == "assistant":
                    if tool_names:
                        tool_str = "+".join(tool_names[:3])
                        # Get output snippet from last tool
                        out_snippet = ""
                        if tool_uses:
                            last_out = tool_uses[-1].get("output", "")
                            if isinstance(last_out, str) and last_out.strip():
                                out_snippet = f"→{_truncate(last_out.strip(), 20)}"
                        label = f"Asst→{tool_str}({_truncate(summary, 15)}){out_snippet}"
                    else:
                        label = f"Asst→{summary}" if summary else "Asst"
                else:
                    continue
                flow_parts.append(label)

            flow_str = ", ".join(flow_parts) if flow_parts else "(empty)"

            # Files
            files = detail.get("files_touched", [])
            if isinstance(files, str):
                try:
                    files = json.loads(files)
                except (json.JSONDecodeError, ValueError):
                    files = []
            files_str = ", ".join(files) if files else "(none)"

            print(f"=== SESSION {idx}/{total}: {sid} ===")
            print(f"Source: {source} | Model: {model} | Project: {project} | {duration} | {total_tokens} tokens")
            print(f"Task: {task_type} | Outcome: {outcome} | {user_msgs} user + {asst_msgs} asst msgs")
            print(f"First msg: \"{_truncate(first_msg, 120)}\"")
            print(f"Flow: {flow_str}")
            print(f"Files: {files_str}")
            print()

        conn.close()
        return

    # Single session mode
    session_ids = args.session_ids or []
    if not session_ids:
        print(json.dumps({"error": "Provide a session_id or use --batch."}))
        conn.close()
        sys.exit(1)

    for sid in session_ids:
        detail = get_session_detail(conn, sid)
        if not detail:
            print(f"Session not found: {sid}")
            continue

        source = detail.get("source", "?")
        model = detail.get("model", "?")
        project = detail.get("project", "?")
        duration = _format_duration(detail.get("duration_seconds"))
        input_tok = _format_tokens(detail.get("input_tokens") or 0)
        output_tok = _format_tokens(detail.get("output_tokens") or 0)
        user_msgs = detail.get("user_messages", 0)
        asst_msgs = detail.get("assistant_messages", 0)
        task_type = detail.get("task_type", "unknown")
        outcome = detail.get("outcome_badge", "unknown")
        sensitivity = detail.get("sensitivity_score", 0.0)

        # Value/risk badges
        value_badges = detail.get("value_badges", [])
        if isinstance(value_badges, str):
            try:
                value_badges = json.loads(value_badges)
            except (json.JSONDecodeError, ValueError):
                value_badges = []
        risk_badges = detail.get("risk_badges", [])
        if isinstance(risk_badges, str):
            try:
                risk_badges = json.loads(risk_badges)
            except (json.JSONDecodeError, ValueError):
                risk_badges = []

        value_str = ", ".join(value_badges) if value_badges else "(none)"
        risk_str = ", ".join(risk_badges) if risk_badges else "(none)"

        print(f"Session: {sid}")
        print(f"Source: {source} | Model: {model} | Project: {project}")
        print(f"Duration: {duration} | Tokens: {input_tok} in / {output_tok} out | Messages: {user_msgs} user / {asst_msgs} asst")
        print(f"Task type: {task_type} | Outcome: {outcome}")
        print(f"Value: {value_str} | Risk: {risk_str} | Sensitivity: {sensitivity}")
        print()

        messages = detail.get("messages", [])

        # First user message
        first_user_text = ""
        for msg in messages:
            if msg.get("role") == "user":
                first_user_text = _get_message_text(msg)
                break

        print("--- FIRST USER MESSAGE ---")
        print(_truncate(first_user_text, 500))
        print()

        # Conversation flow
        print("--- CONVERSATION FLOW ---")
        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            if role == "user":
                role_label = "User"
            elif role == "assistant":
                role_label = "Asst"
            else:
                continue

            text = _get_message_text(msg)
            print(f"#{i} [{role_label}] {_truncate(text, 80)}")

            tool_uses = _extract_tool_uses(msg)
            for tu in tool_uses:
                tool_name = tu.get("tool", "?")
                inp = tu.get("input", {})
                first_arg = tu.get("first_arg", "")
                if not first_arg and isinstance(inp, dict):
                    for v in inp.values():
                        if isinstance(v, str) and v.strip():
                            first_arg = v.strip()
                            break
                first_arg_str = f"({_truncate(first_arg, 30)})" if first_arg else "()"
                status_str = tu.get("status", "")
                output = tu.get("output", "")
                output_str = ""
                if isinstance(output, str) and output.strip():
                    output_str = f" — \"{_truncate(output.strip(), 40)}\""
                status_display = f" {status_str}" if status_str else ""
                print(f"   → {tool_name}{first_arg_str}{status_display}{output_str}")
        print()

        # Files touched
        files = detail.get("files_touched", [])
        if isinstance(files, str):
            try:
                files = json.loads(files)
            except (json.JSONDecodeError, ValueError):
                files = []
        print("--- FILES TOUCHED ---")
        print(", ".join(files) if files else "(none)")
        print()

        # Commands run
        commands = detail.get("commands_run", [])
        if isinstance(commands, str):
            try:
                commands = json.loads(commands)
            except (json.JSONDecodeError, ValueError):
                commands = []
        print("--- COMMANDS RUN ---")
        if commands:
            for cmd in commands:
                print(cmd)
        else:
            print("(none)")
        print()

    conn.close()


def _run_set_score(args) -> None:
    """Record AI quality score for one or more sessions."""
    from .index import open_index, update_session

    session_ids = args.session_ids
    quality = args.quality
    reason = args.reason

    if not session_ids:
        print(json.dumps({"error": "No session IDs provided."}))
        sys.exit(1)

    conn = open_index()
    results = []
    for sid in session_ids:
        ok = update_session(
            conn, sid,
            ai_quality_score=quality,
            ai_score_reason=reason,
        )
        results.append({"session_id": sid, "ai_quality_score": quality, "ok": ok})
    conn.close()

    success = sum(1 for r in results if r["ok"])
    print(json.dumps({
        "action": "set-score",
        "updated": success,
        "quality": quality,
        "results": results,
    }, indent=2))


def _run_score_batch(args) -> None:
    """List unscored sessions as JSON."""
    from .index import open_index, query_unscored_sessions

    conn = open_index()
    sessions = query_unscored_sessions(conn, limit=args.limit, source=args.source)
    conn.close()

    print(json.dumps(sessions, indent=2))


def _generate_score_view_text(conn, session_id: str) -> str | None:
    """Generate score-view text for a session as a string (for piping to claude -p)."""
    from .index import get_session_detail

    detail = get_session_detail(conn, session_id)
    if not detail:
        return None

    lines: list[str] = []

    source = detail.get("source", "?")
    model = detail.get("model", "?")
    project = detail.get("project", "?")
    duration = _format_duration(detail.get("duration_seconds"))
    input_tok = _format_tokens(detail.get("input_tokens") or 0)
    output_tok = _format_tokens(detail.get("output_tokens") or 0)
    user_msgs = detail.get("user_messages", 0)
    asst_msgs = detail.get("assistant_messages", 0)
    task_type = detail.get("task_type", "unknown")
    outcome = detail.get("outcome_badge", "unknown")
    sensitivity = detail.get("sensitivity_score", 0.0)

    value_badges = detail.get("value_badges", [])
    if isinstance(value_badges, str):
        try:
            value_badges = json.loads(value_badges)
        except (json.JSONDecodeError, ValueError):
            value_badges = []
    risk_badges = detail.get("risk_badges", [])
    if isinstance(risk_badges, str):
        try:
            risk_badges = json.loads(risk_badges)
        except (json.JSONDecodeError, ValueError):
            risk_badges = []

    value_str = ", ".join(value_badges) if value_badges else "(none)"
    risk_str = ", ".join(risk_badges) if risk_badges else "(none)"

    lines.append(f"Session: {session_id}")
    lines.append(f"Source: {source} | Model: {model} | Project: {project}")
    lines.append(f"Duration: {duration} | Tokens: {input_tok} in / {output_tok} out | Messages: {user_msgs} user / {asst_msgs} asst")
    lines.append(f"Task type: {task_type} | Outcome: {outcome}")
    lines.append(f"Value: {value_str} | Risk: {risk_str} | Sensitivity: {sensitivity}")
    lines.append("")

    messages = detail.get("messages", [])

    # First user message
    first_user_text = ""
    for msg in messages:
        if msg.get("role") == "user":
            first_user_text = _get_message_text(msg)
            break

    lines.append("--- FIRST USER MESSAGE ---")
    lines.append(_truncate(first_user_text, 500))
    lines.append("")

    # Conversation flow
    lines.append("--- CONVERSATION FLOW ---")
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        if role == "user":
            role_label = "User"
        elif role == "assistant":
            role_label = "Asst"
        else:
            continue

        text = _get_message_text(msg)
        lines.append(f"#{i} [{role_label}] {_truncate(text, 80)}")

        tool_uses = _extract_tool_uses(msg)
        for tu in tool_uses:
            tool_name = tu.get("tool", "?")
            inp = tu.get("input", {})
            first_arg = tu.get("first_arg", "")
            if not first_arg and isinstance(inp, dict):
                for v in inp.values():
                    if isinstance(v, str) and v.strip():
                        first_arg = v.strip()
                        break
            first_arg_str = f"({_truncate(first_arg, 30)})" if first_arg else "()"
            status_str = tu.get("status", "")
            output = tu.get("output", "")
            output_str = ""
            if isinstance(output, str) and output.strip():
                output_str = f' — "{_truncate(output.strip(), 40)}"'
            status_display = f" {status_str}" if status_str else ""
            lines.append(f"   → {tool_name}{first_arg_str}{status_display}{output_str}")
    lines.append("")

    # Files touched
    files = detail.get("files_touched", [])
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except (json.JSONDecodeError, ValueError):
            files = []
    lines.append("--- FILES TOUCHED ---")
    lines.append(", ".join(files) if files else "(none)")
    lines.append("")

    # Commands run
    commands = detail.get("commands_run", [])
    if isinstance(commands, str):
        try:
            commands = json.loads(commands)
        except (json.JSONDecodeError, ValueError):
            commands = []
    lines.append("--- COMMANDS RUN ---")
    if commands:
        for cmd in commands:
            lines.append(cmd)
    else:
        lines.append("(none)")
    lines.append("")

    return "\n".join(lines)


_SCORE_RUBRIC = """\
Score this coding agent session for quality (1-5).

Rubric:
5 = Excellent: Clear non-trivial coding task. Verified outcome (tests pass, code compiles). Rich tool usage, multi-step problem-solving.
4 = Good: Clear task, useful outcome. Some tool usage and verification.
3 = Average: Routine task. Partial/unverified outcome. Basic interaction.
2 = Low: Vague/trivial task. Failed or unclear outcome. Minimal interaction.
1 = Poor: No discernible coding task. Trivially short or broken.

Evaluate: intent clarity, outcome success, conversation substance, agent quality.
For Claude Code: value IDE workflows (read→edit→test), bash usage, multi-file changes, debugging with resolution.
For Codex: value clear specs, multi-step implementations."""

_SCORE_JSON_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "quality": {"type": "integer", "minimum": 1, "maximum": 5},
        "reason": {"type": "string"},
    },
    "required": ["quality", "reason"],
})


def _score_single_session(
    conn,
    session_id: str,
    *,
    model: str = "sonnet",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Score a single session using claude -p. Returns result dict."""
    import subprocess

    score_view_text = _generate_score_view_text(conn, session_id)
    if score_view_text is None:
        return {"session_id": session_id, "error": "Session not found"}

    if dry_run:
        print(score_view_text, file=sys.stderr)
        return {"session_id": session_id, "dry_run": True}

    cmd = [
        "claude", "-p",
        "--append-system-prompt", _SCORE_RUBRIC,
        "--json-schema", _SCORE_JSON_SCHEMA,
        "--output-format", "json",
        "--tools", "",
        "--no-session-persistence",
        "--model", model,
        "Score this coding agent session for quality (1-5). Evaluate intent clarity, outcome success, conversation substance, and agent quality.",
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=score_view_text,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        return {"session_id": session_id, "error": "claude CLI not found. Install Claude Code first."}
    except subprocess.TimeoutExpired:
        return {"session_id": session_id, "error": "Timed out waiting for claude"}

    if proc.returncode != 0:
        stderr = proc.stderr.strip() if proc.stderr else ""
        return {"session_id": session_id, "error": f"claude exited {proc.returncode}: {stderr}"}

    # Parse response — claude --output-format json returns a JSON object
    # with a "result" field containing the text, and when --json-schema is used
    # it returns structured_output
    try:
        response = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"session_id": session_id, "error": f"Failed to parse claude response: {proc.stdout[:200]}"}

    # Extract the structured output
    structured = response.get("structured_output")
    if structured is None:
        # Try parsing result text as JSON
        result_text = response.get("result", "")
        try:
            structured = json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            return {"session_id": session_id, "error": f"No structured output in response"}

    quality = structured.get("quality")
    reason = structured.get("reason", "")

    if not isinstance(quality, int) or not (1 <= quality <= 5):
        return {"session_id": session_id, "error": f"Invalid quality score: {quality}"}

    # Store the score
    from .index import update_session
    ok = update_session(conn, session_id, ai_quality_score=quality, ai_score_reason=reason)

    return {
        "session_id": session_id,
        "ai_quality_score": quality,
        "reason": reason,
        "ok": ok,
    }


def _run_score(args) -> None:
    """Score sessions using claude -p for automated AI evaluation."""
    from .index import open_index, query_unscored_sessions, update_session

    conn = open_index()

    model = args.model
    dry_run = args.dry_run
    batch = args.batch
    auto_triage = getattr(args, "auto_triage", False)

    if batch:
        # Batch mode: score unscored sessions
        sessions = query_unscored_sessions(conn, limit=args.limit, source=args.source)
        if not sessions:
            print(json.dumps({"message": "No unscored sessions found.", "scored": 0}))
            conn.close()
            return

        results = []
        for i, s in enumerate(sessions, 1):
            sid = s["session_id"]
            title = s.get("display_title", sid)
            print(f"[{i}/{len(sessions)}] Scoring: {_truncate(title, 60)} ({sid[:12]}...)", file=sys.stderr)
            result = _score_single_session(conn, sid, model=model, dry_run=dry_run)
            results.append(result)
            if result.get("ai_quality_score"):
                print(f"  -> {result['ai_quality_score']}/5: {result.get('reason', '')}", file=sys.stderr)
            elif result.get("error"):
                print(f"  -> Error: {result['error']}", file=sys.stderr)
            elif result.get("dry_run"):
                print(f"  -> (dry run)", file=sys.stderr)

        scored = [r for r in results if r.get("ok")]
        errors = [r for r in results if r.get("error")]
        summary = {
            "scored": len(scored),
            "errors": len(errors),
            "results": results,
        }
        if scored:
            scores = [r["ai_quality_score"] for r in scored]
            summary["score_distribution"] = {
                "excellent_5": sum(1 for q in scores if q == 5),
                "good_4": sum(1 for q in scores if q == 4),
                "average_3": sum(1 for q in scores if q == 3),
                "low_2": sum(1 for q in scores if q == 2),
                "poor_1": sum(1 for q in scores if q == 1),
            }

        # Auto-triage: approve 4-5, block 1-2, leave 3 for manual review
        if auto_triage and scored and not dry_run:
            approve_ids = [r["session_id"] for r in scored if r["ai_quality_score"] >= 4]
            block_ids = [r["session_id"] for r in scored if r["ai_quality_score"] <= 2]
            triage = {"approved": 0, "blocked": 0, "manual_review": 0}
            if approve_ids:
                for sid in approve_ids:
                    reason = next(
                        (r.get("reason", "Auto-triage: high quality") for r in scored if r["session_id"] == sid),
                        "Auto-triage: high quality",
                    )
                    update_session(conn, sid, status="approved", reason=reason)
                triage["approved"] = len(approve_ids)
                print(f"  Auto-approved {len(approve_ids)} sessions (score 4-5)", file=sys.stderr)
            if block_ids:
                for sid in block_ids:
                    reason = next(
                        (r.get("reason", "Auto-triage: low quality") for r in scored if r["session_id"] == sid),
                        "Auto-triage: low quality",
                    )
                    update_session(conn, sid, status="blocked", reason=reason)
                triage["blocked"] = len(block_ids)
                print(f"  Auto-blocked {len(block_ids)} sessions (score 1-2)", file=sys.stderr)
            triage["manual_review"] = sum(1 for r in scored if r["ai_quality_score"] == 3)
            summary["auto_triage"] = triage

        print(json.dumps(summary, indent=2))
    else:
        # Single session mode
        session_ids = args.session_ids or []
        if not session_ids:
            print(json.dumps({"error": "Provide session ID(s) or use --batch"}))
            conn.close()
            sys.exit(1)

        results = []
        for sid in session_ids:
            result = _score_single_session(conn, sid, model=model, dry_run=dry_run)
            results.append(result)

        if len(results) == 1:
            print(json.dumps(results[0], indent=2))
        else:
            print(json.dumps({"results": results}, indent=2))

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="DataClaw — Claude/Codex -> Hugging Face")
    sub = parser.add_subparsers(dest="command")

    prep_parser = sub.add_parser("prep", help="Data prep — discover projects, detect HF, output JSON")
    prep_parser.add_argument("--source", choices=SOURCE_CHOICES, default="auto")
    sub.add_parser("status", help="Show current stage and next steps (JSON)")
    cf = sub.add_parser("confirm", help="Scan for PII, summarize export, and unlock pushing (JSON)")
    cf.add_argument("--file", "-f", type=Path, default=None, help="Path to export JSONL file")
    cf.add_argument("--full-name", type=str, default=None,
                    help="User's full name to scan for in the export file (exact-name privacy check).")
    cf.add_argument("--skip-full-name-scan", action="store_true",
                    help="Skip exact full-name scan when the user declines sharing their name.")
    cf.add_argument("--attest-full-name", type=str, default=None,
                    help="Text attestation describing how full-name scan was done.")
    cf.add_argument("--attest-sensitive", type=str, default=None,
                    help="Text attestation describing sensitive-entity review and outcome.")
    cf.add_argument("--attest-manual-scan", type=str, nargs="?", const="__DEPRECATED_FLAG__", default=None,
                    help=f"Text attestation describing manual scan ({MIN_MANUAL_SCAN_SESSIONS}+ sessions).")
    # Deprecated boolean attestations retained only for a guided migration error.
    cf.add_argument("--attest-asked-full-name", action="store_true", help=argparse.SUPPRESS)
    cf.add_argument("--attest-asked-sensitive", action="store_true", help=argparse.SUPPRESS)
    cf.add_argument("--attest-asked-manual-scan", action="store_true", help=argparse.SUPPRESS)
    list_parser = sub.add_parser("list", help="List all projects")
    list_parser.add_argument("--source", choices=SOURCE_CHOICES, default="auto")

    us = sub.add_parser("update-skill", help="Install/update the dataclaw skill for a coding agent")
    us.add_argument("target", choices=["claude", "openclaw", "codex", "cline"],
                    help="Agent to install skill for")

    cfg = sub.add_parser("config", help="View or set config")
    cfg.add_argument("--repo", type=str, help="Set HF repo")
    cfg.add_argument("--source", choices=sorted(EXPLICIT_SOURCE_CHOICES),
                     help="Set export source scope explicitly: claude, codex, gemini, or all")
    cfg.add_argument("--exclude", type=str, help="Comma-separated projects to exclude")
    cfg.add_argument("--redact", type=str,
                     help="Comma-separated strings to always redact (API keys, usernames, domains)")
    cfg.add_argument("--redact-usernames", type=str,
                     help="Comma-separated usernames to anonymize (GitHub handles, Discord names)")
    cfg.add_argument("--confirm-projects", action="store_true",
                     help="Mark project selection as confirmed (include all)")

    # Workbench commands
    serve_parser = sub.add_parser("serve", help="Start the workbench daemon + web UI")
    serve_parser.add_argument("--port", type=int, default=8384, help="Port (default: 8384)")
    serve_parser.add_argument("--no-browser", action="store_true", help="Don't open browser")
    serve_parser.add_argument("--source", choices=["claude", "codex", "openclaw"], default=None,
                              help="Only scan this source")

    scan_parser = sub.add_parser("scan", help="One-shot index sessions into local workbench DB")
    scan_parser.add_argument("--source", choices=["claude", "codex", "openclaw"], default=None,
                             help="Only scan this source")

    inbox_parser = sub.add_parser("inbox", help="List indexed sessions in terminal")
    inbox_parser.add_argument("--status", choices=["new", "shortlisted", "approved", "blocked"],
                              default=None)
    inbox_parser.add_argument("--source", choices=["claude", "codex", "openclaw"], default=None)
    inbox_parser.add_argument("--limit", type=int, default=20)
    inbox_parser.add_argument("--json", action="store_true", help="Output JSON for agent parsing")

    # Review action commands
    for action_name in ("approve", "block", "shortlist"):
        action_parser = sub.add_parser(action_name, help=f"{action_name.title()} sessions by ID")
        action_parser.add_argument("session_ids", nargs="+", help="Session IDs to update")
        action_parser.add_argument("--reason", type=str, default=None, help="Reason for the action")

    # Scoring commands
    sv = sub.add_parser("score-view", help="Show condensed session view for AI scoring")
    sv.add_argument("session_ids", nargs="*", help="Session IDs to view")
    sv.add_argument("--batch", action="store_true", help="Compact batch format")
    sv.add_argument("--limit", type=int, default=5, help="Sessions per batch")
    sv.add_argument("--offset", type=int, default=0, help="Offset for batch")
    sv.add_argument("--source", choices=["claude", "codex", "openclaw"], default=None)

    ss = sub.add_parser("set-score", help="Record AI quality score for sessions")
    ss.add_argument("session_ids", nargs="+", help="Session IDs")
    ss.add_argument("--quality", type=int, required=True, choices=range(1, 6), help="Quality 1-5")
    ss.add_argument("--reason", type=str, default=None, help="Reason for the score")

    sb = sub.add_parser("score-batch", help="List unscored sessions for AI scoring")
    sb.add_argument("--limit", type=int, default=50)
    sb.add_argument("--source", choices=["claude", "codex", "openclaw"], default=None)

    sc = sub.add_parser("score", help="Auto-score sessions via claude -p")
    sc.add_argument("session_ids", nargs="*", help="Session IDs to score")
    sc.add_argument("--batch", action="store_true", help="Score all unscored sessions")
    sc.add_argument("--limit", type=int, default=100, help="Max sessions for batch mode")
    sc.add_argument("--source", choices=["claude", "codex", "openclaw"], default=None)
    sc.add_argument("--model", type=str, default="sonnet", help="Model for scoring (default: sonnet)")
    sc.add_argument("--dry-run", action="store_true", help="Show score-view without calling claude")
    sc.add_argument("--auto-triage", action="store_true",
                    help="After scoring, auto-approve 4-5 and auto-block 1-2 (score 3 left for review)")

    exp = sub.add_parser("export", help="Export locally (default). Use --push to upload to HF.")
    # Export flags on both the subcommand and root parser so `dataclaw --push` works
    for target in (exp, parser):
        target.add_argument("--output", "-o", type=Path, default=None)
        target.add_argument("--repo", "-r", type=str, default=None)
        target.add_argument("--source", choices=SOURCE_CHOICES, default="auto")
        target.add_argument("--all-projects", action="store_true")
        target.add_argument("--no-thinking", action="store_true")
        target.add_argument("--push", action="store_true",
                            help="Upload to Hugging Face after export (requires dataclaw confirm first)")
        target.add_argument("--no-push", action="store_true",
                            help="(Default, kept for backwards compatibility) Export locally only")
        target.add_argument(
            "--publish-attestation",
            type=str,
            default=None,
            help="Required for push: text attestation that user explicitly approved publishing.",
        )
        target.add_argument("--attest-user-approved-publish", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args()
    command = args.command or "export"

    if command == "serve":
        from .daemon import run_server
        run_server(
            port=args.port,
            open_browser=not args.no_browser,
            source_filter=args.source,
        )
        return

    if command == "scan":
        _run_scan(source_filter=args.source)
        return

    if command == "inbox":
        _run_inbox(status=args.status, source=args.source, limit=args.limit,
                   output_json=args.json)
        return

    if command in ("approve", "block", "shortlist"):
        status_map = {"approve": "approved", "block": "blocked", "shortlist": "shortlisted"}
        _run_review_action(status_map[command], args.session_ids, reason=args.reason)
        return

    if command == "score-view":
        _run_score_view(args)
        return

    if command == "set-score":
        _run_set_score(args)
        return

    if command == "score-batch":
        _run_score_batch(args)
        return

    if command == "score":
        _run_score(args)
        return

    if command == "prep":
        prep(source_filter=args.source)
        return

    if command == "status":
        status()
        return

    if command == "confirm":
        if (
            args.attest_asked_full_name
            or args.attest_asked_sensitive
            or args.attest_asked_manual_scan
            or args.attest_manual_scan == "__DEPRECATED_FLAG__"
        ):
            print(json.dumps({
                "error": "Deprecated boolean attestation flags were provided.",
                "hint": (
                    "Use text attestations instead so the command can validate what was reviewed."
                ),
                "blocked_on_step": "Step 2/3",
                "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                "next_command": CONFIRM_COMMAND_EXAMPLE,
            }, indent=2))
            sys.exit(1)
        confirm(
            file_path=args.file,
            full_name=args.full_name,
            attest_asked_full_name=args.attest_full_name,
            attest_asked_sensitive=args.attest_sensitive,
            attest_manual_scan=args.attest_manual_scan,
            skip_full_name_scan=args.skip_full_name_scan,
        )
        return

    if command == "update-skill":
        update_skill(args.target)
        return

    if command == "list":
        config = load_config()
        resolved_source_choice, _ = _resolve_source_choice(args.source, config)
        list_projects(source_filter=resolved_source_choice)
        return

    if command == "config":
        _handle_config(args)
        return

    _run_export(args)


def _parse_csv_arg(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _handle_config(args) -> None:
    """Handle the config subcommand."""
    has_changes = (
        args.repo
        or args.source
        or args.exclude
        or args.redact
        or args.redact_usernames
        or args.confirm_projects
    )
    if not has_changes:
        print(json.dumps(_mask_config_for_display(load_config()), indent=2))
        return
    configure(
        repo=args.repo,
        source=args.source,
        exclude=_parse_csv_arg(args.exclude),
        redact=_parse_csv_arg(args.redact),
        redact_usernames=_parse_csv_arg(args.redact_usernames),
        confirm_projects=args.confirm_projects or bool(args.exclude),
    )


def _run_export(args) -> None:
    """Run the export flow — discover, anonymize, export, optionally push."""
    config = load_config()
    source_choice, source_explicit = _resolve_source_choice(args.source, config)
    source_filter = _normalize_source_filter(source_choice)

    if not source_explicit:
        print(json.dumps({
            "error": "Source scope is not confirmed yet.",
            "hint": (
                "Explicitly choose one source scope before exporting: "
                "`claude`, `codex`, `gemini`, or `all`."
            ),
            "required_action": (
                "Ask the user whether to export Claude Code, Codex, Gemini, or all. "
                "Then run `dataclaw config --source <claude|codex|gemini|all>` "
                "or pass `--source <claude|codex|gemini|all>` on the export command."
            ),
            "allowed_sources": sorted(EXPLICIT_SOURCE_CHOICES),
            "blocked_on_step": "Step 2/6",
            "process_steps": SETUP_TO_PUBLISH_STEPS,
            "next_command": "dataclaw config --source all",
        }, indent=2))
        sys.exit(1)

    # Gate: require `dataclaw confirm` before pushing
    # Default is local-only. Push only when --push is explicitly passed.
    wants_push = getattr(args, "push", False) and not args.no_push
    if wants_push:
        print(json.dumps({
            "error": "Uploading to Hugging Face is temporarily disabled.",
            "hint": "Use 'dataclaw export' to export locally.",
        }, indent=2))
        sys.exit(1)
    if False:  # HF upload disabled — preserved for future re-enable
        if args.attest_user_approved_publish and not args.publish_attestation:
            print(json.dumps({
                "error": "Deprecated publish attestation flag was provided.",
                "hint": "Use --publish-attestation with a detailed text statement.",
                "blocked_on_step": "Step 3/3",
                "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                "next_command": (
                    "dataclaw export --publish-attestation "
                    "\"User explicitly approved publishing to Hugging Face on YYYY-MM-DD.\""
                ),
            }, indent=2))
            sys.exit(1)
        if config.get("stage") != "confirmed":
            print(json.dumps({
                "error": "You must run `dataclaw confirm` before pushing.",
                "hint": "Export first with --no-push, review the data, then run `dataclaw confirm`.",
                "blocked_on_step": "Step 2/3",
                "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                "next_command": "dataclaw confirm",
            }, indent=2))
            sys.exit(1)
        publish_attestation, publish_error = _validate_publish_attestation(args.publish_attestation)
        if publish_error:
            print(json.dumps({
                "error": "Missing or invalid publish attestation.",
                "publish_attestation_error": publish_error,
                "hint": "Ask the user to explicitly approve publishing, then pass a detailed text attestation.",
                "blocked_on_step": "Step 3/3",
                "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                "next_command": (
                    "dataclaw export --publish-attestation "
                    "\"User explicitly approved publishing to Hugging Face on YYYY-MM-DD.\""
                ),
            }, indent=2))
            sys.exit(1)

        review_attestations = config.get("review_attestations", {})
        review_verification = config.get("review_verification", {})
        verified_full_name = _normalize_attestation_text(review_verification.get("full_name"))
        _, review_errors, _ = _collect_review_attestations(
            attest_asked_full_name=review_attestations.get("asked_full_name"),
            attest_asked_sensitive=review_attestations.get("asked_sensitive_entities"),
            attest_manual_scan=review_attestations.get("manual_scan_done"),
            full_name=verified_full_name if verified_full_name else None,
            skip_full_name_scan=bool(review_verification.get("full_name_scan_skipped", False)),
        )
        if not verified_full_name and not review_verification.get("full_name_scan_skipped", False):
            review_errors["asked_full_name"] = (
                "Missing verified full-name scan from confirm step; rerun confirm (or use --skip-full-name-scan if the user declined)."
            )
        verified_manual_count = review_verification.get("manual_scan_sessions")
        if not isinstance(verified_manual_count, int) or verified_manual_count < MIN_MANUAL_SCAN_SESSIONS:
            review_errors["manual_scan_done"] = (
                "Missing verified manual scan evidence from confirm step; rerun confirm."
            )

        if review_errors:
            print(json.dumps({
                "error": "Missing or invalid review attestations from confirm step.",
                "attestation_errors": review_errors,
                "blocked_on_step": "Step 2/3",
                "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                "next_command": CONFIRM_COMMAND_EXAMPLE,
            }, indent=2))
            sys.exit(1)

        config["publish_attestation"] = publish_attestation
        save_config(config)

    print("=" * 50)
    print("  DataClaw — Claude/Codex Log Exporter")
    print("=" * 50)

    if not _has_session_sources(source_filter):
        if source_filter == "claude":
            print(f"Error: {CLAUDE_DIR} not found.", file=sys.stderr)
        elif source_filter == "codex":
            print(f"Error: {CODEX_DIR} not found.", file=sys.stderr)
        elif source_filter == "gemini":
            from .parser import GEMINI_DIR
            print(f"Error: {GEMINI_DIR} not found.", file=sys.stderr)
        else:
            print("Error: none of ~/.claude, ~/.codex, or ~/.gemini/tmp were found.", file=sys.stderr)
        sys.exit(1)

    projects = _filter_projects_by_source(discover_projects(), source_filter)
    if not projects:
        print(f"No {_source_label(source_filter)} sessions found.", file=sys.stderr)
        sys.exit(1)

    if not args.all_projects and not config.get("projects_confirmed", False):
        excluded = set(config.get("excluded_projects", []))
        list_command = f"dataclaw list --source {source_choice}"
        print(json.dumps({
            "error": "Project selection is not confirmed yet.",
            "hint": (
                f"Run `{list_command}`, present the full project list to the user, discuss which projects to exclude, then run "
                "`dataclaw config --exclude \"p1,p2\"` or `dataclaw config --confirm-projects`."
            ),
            "required_action": (
                "Send the full project/folder list below to the user in a message and get explicit "
                "confirmation on exclusions before exporting."
            ),
            "projects": [
                {
                    "name": p["display_name"],
                    "source": p.get("source", "claude"),
                    "sessions": p["session_count"],
                    "size": _format_size(p["total_size_bytes"]),
                    "excluded": p["display_name"] in excluded,
                }
                for p in projects
            ],
            "blocked_on_step": "Step 3/6",
            "process_steps": SETUP_TO_PUBLISH_STEPS,
            "next_command": "dataclaw config --confirm-projects",
        }, indent=2))
        sys.exit(1)

    total_sessions = sum(p["session_count"] for p in projects)
    total_size = sum(p["total_size_bytes"] for p in projects)
    print(f"\nFound {total_sessions} sessions across {len(projects)} projects "
          f"({_format_size(total_size)} raw)")
    print(f"Source scope: {source_choice}")

    # Resolve repo — CLI flag > config > auto-detect from HF username
    repo_id = args.repo or config.get("repo")
    if not repo_id and wants_push:
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
    anonymizer = Anonymizer(extra_usernames=extra_usernames)

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
        "source": source_choice,
    }
    if not wants_push:
        config["stage"] = "review"
    save_config(config)

    if not wants_push:
        print(f"\nDone! JSONL file: {output_path}")
        abs_path = str(output_path.resolve())
        next_steps, next_command = _build_status_next_steps("review", config, None, None)
        json_block = {
            "stage": "review",
            "stage_number": 3,
            "total_stages": 4,
            "sessions": meta["sessions"],
            "source": source_choice,
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
        print(f"Or set manually: dataclaw config --repo username/my-personal-codex-data")
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
    print("NEXT: Ask for full name to run an exact-name privacy check, then scan for it:")
    print(f"  grep -i 'THEIR_NAME' {abs_output} | head -10")
    print("  If user declines sharing full name: use dataclaw confirm --skip-full-name-scan with a skip attestation.")
    print()
    print("To add custom redactions, then re-export:")
    print("  dataclaw config --redact-usernames 'github_handle,discord_name'")
    print("  dataclaw config --redact 'secret-domain.com,my-api-key'")
    print(f"  dataclaw export --no-push -o {abs_output}")
    print()
    print(f"Found an issue? Help improve DataClaw: {REPO_URL}/issues")


if __name__ == "__main__":
    main()
