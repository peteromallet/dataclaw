import logging
from collections.abc import Iterable
from pathlib import Path

from .. import _json as json
from ..anonymizer import Anonymizer
from ..secrets import redact_text

logger = logging.getLogger(__name__)

SOURCE = "custom"
CUSTOM_DIR = Path.home() / ".dataclaw" / "custom"


def discover_projects(custom_dir: Path | None = None) -> list[dict]:
    if custom_dir is None:
        custom_dir = CUSTOM_DIR
    if not custom_dir.exists():
        return []

    projects = []
    for project_dir in sorted(custom_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        saw_file = False
        session_count = 0
        total_size = 0
        for jsonl_file in sorted(project_dir.glob("*.jsonl")):
            saw_file = True
            total_size += jsonl_file.stat().st_size
            try:
                session_count += sum(1 for line in jsonl_file.open() if line.strip())
            except OSError as e:
                logger.warning("Failed to read %s: %s", jsonl_file, e)
        if not saw_file:
            continue
        if session_count == 0:
            continue
        projects.append(
            {
                "dir_name": project_dir.name,
                "display_name": f"custom:{project_dir.name}",
                "session_count": session_count,
                "total_size_bytes": total_size,
                "source": SOURCE,
            }
        )
    return projects


def parse_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
    custom_dir: Path | None = None,
) -> Iterable[dict]:
    if custom_dir is None:
        custom_dir = CUSTOM_DIR
    project_path = custom_dir / project_dir_name
    if not project_path.exists():
        return

    required_fields = {"session_id", "model", "messages"}
    for jsonl_file in sorted(project_path.glob("*.jsonl")):
        try:
            for line_num, line in enumerate(jsonl_file.open(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    session = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "custom:%s: %s line %d: invalid JSON, skipping",
                        project_dir_name,
                        jsonl_file.name,
                        line_num,
                    )
                    continue
                if not isinstance(session, dict):
                    logger.warning(
                        "custom:%s: %s line %d: not a JSON object, skipping",
                        project_dir_name,
                        jsonl_file.name,
                        line_num,
                    )
                    continue
                missing = required_fields - session.keys()
                if missing:
                    logger.warning(
                        "custom:%s: %s line %d: missing required fields %s, skipping",
                        project_dir_name,
                        jsonl_file.name,
                        line_num,
                        sorted(missing),
                    )
                    continue
                session["project"] = f"custom:{project_dir_name}"
                session["source"] = SOURCE
                for msg in session.get("messages", []):
                    if "content" in msg and isinstance(msg["content"], str):
                        redacted, _ = redact_text(msg["content"])
                        msg["content"] = anonymizer.text(redacted)
                yield session
        except OSError:
            logger.warning("custom:%s: failed to read %s", project_dir_name, jsonl_file.name)


def parse_sessions(project_dir_name: str, custom_dir: Path, anonymizer: Anonymizer) -> list[dict]:
    return list(
        parse_project_sessions(
            project_dir_name,
            anonymizer,
            include_thinking=True,
            custom_dir=custom_dir,
        )
    )
