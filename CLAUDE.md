# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DataClaw exports coding agent conversation history (Claude Code, Codex, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, Cline) to Hugging Face as structured JSONL datasets. It parses session logs, redacts secrets and PII, and uploads the result.

## Commands

```bash
# Install in dev mode
pip install -e ".[dev]"

# Run all tests
pytest

# Run a single test file
pytest tests/test_parser.py

# Run a single test by name
pytest tests/test_secrets.py -k "test_redact_jwt"

# Run the CLI
dataclaw --help
```

CI runs `pytest tests/ -v` on Python 3.10–3.13 via GitHub Actions (`.github/workflows/test.yml`).

## Architecture

The codebase is a single Python package (`dataclaw/`) with a CLI entry point at `dataclaw.cli:main`.

**Module responsibilities:**

- **cli.py** — All CLI commands (prep, config, list, export, confirm, status, update-skill). Uses argparse. Orchestrates the multi-step export workflow with stage gating (auth → configure → review → confirmed → done). Handles Hugging Face upload via `huggingface_hub`.
- **parser.py** — Discovers projects and parses session data from each source's local storage format. Each source has its own directory layout:
  - Claude Code: `~/.claude/projects/` (JSONL files)
  - Codex: `~/.codex/sessions/` (JSONL files)
  - Gemini CLI: `~/.gemini/tmp/` (JSON files, project dirs named by SHA-256 of working directory)
  - OpenCode: `~/.local/share/opencode/opencode.db` (SQLite)
  - OpenClaw: `~/.openclaw/agents/` (JSONL files)
  - Kimi CLI: `~/.kimi/sessions/` (JSONL files)
  - Cline: `~/.cline/data/tasks/` (JSON files, Anthropic MessageParam format). Also scans legacy VS Code extension paths on macOS/Linux/Windows.
  - Custom: `~/.dataclaw/custom/` (user-provided JSONL)
- **anonymizer.py** — Path anonymization (strips to project-relative) and username hashing. The `Anonymizer` class supports extra usernames beyond the OS user.
- **secrets.py** — Regex-based secret detection (JWT, API keys, DB URLs, etc.) with entropy analysis for high-entropy strings. Includes an allowlist for common false positives. `redact_session()` processes entire session dicts recursively.
- **config.py** — Persistent config at `~/.dataclaw/config.json`. Simple load/save with `DataClawConfig` TypedDict.

**Key data flow:** `discover_projects()` → `parse_project_sessions()` → `Anonymizer` + `redact_session()` → JSONL export → HF upload.

**Stage gating:** The CLI enforces a strict workflow order. Export requires source selection and project confirmation. Push requires running `confirm` with attestation flags first. All CLI commands output JSON with `next_steps` fields — the AGENTS.md file documents this "follow the next_steps" pattern that agents must adhere to.

**Adding a new source:** Each source needs: a source constant, directory paths, an entry in `discover_projects()`, a parsing branch in `parse_project_sessions()`, and the source string added to `EXPLICIT_SOURCE_CHOICES`/`SOURCE_CHOICES` in cli.py.

## Testing Conventions

- Tests mirror module structure: `test_cli.py`, `test_parser.py`, `test_anonymizer.py`, `test_secrets.py`, `test_config.py`
- `conftest.py` provides shared fixtures: `sample_user_entry`, `sample_assistant_entry`, `mock_anonymizer` (patches `_detect_home_dir`), `tmp_config` (monkeypatches config paths to tmp_path)
- Tests use `monkeypatch` and `unittest.mock` for isolation — no real filesystem or HF access

## Important Details

- Only dependency is `huggingface_hub>=0.20.0` (plus pytest for dev)
- Python 3.10+ required (uses `X | Y` union syntax)
- Version is tracked in both `pyproject.toml` and `dataclaw/__init__.py` — keep them in sync
- The `dataclaw` tag is applied to all HF datasets for discoverability
