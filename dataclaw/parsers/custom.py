import logging
from collections.abc import Iterable
from pathlib import Path

from .. import _json as json
from ..anonymizer import Anonymizer
from ..export_tasks import ExportSessionTask
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

    for jsonl_file in sorted(project_path.glob("*.jsonl")):
        try:
            for line in jsonl_file.open():
                session = parse_session_bytes(project_dir_name, line, anonymizer)
                if session is not None:
                    yield session
        except OSError:
            logger.warning("custom:%s: failed to read %s", project_dir_name, jsonl_file.name)


def build_export_session_tasks(project_index: int, project: dict) -> list[ExportSessionTask]:
    project_path = CUSTOM_DIR / project["dir_name"]
    if not project_path.exists():
        return []

    tasks: list[ExportSessionTask] = []
    task_index = 0
    for jsonl_file in sorted(project_path.glob("*.jsonl")):
        with open(jsonl_file, "rb") as fh:
            while True:
                offset = fh.tell()
                line = fh.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                tasks.append(
                    ExportSessionTask(
                        source=SOURCE,
                        project_index=project_index,
                        task_index=task_index,
                        project_dir_name=project["dir_name"],
                        project_display_name=project["display_name"],
                        estimated_bytes=len(line),
                        kind="custom-line",
                        file_path=str(jsonl_file),
                        offset=offset,
                        length=len(line),
                    )
                )
                task_index += 1
    return tasks


def parse_export_session_task(
    task: ExportSessionTask,
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> dict | None:
    del include_thinking
    if not task.file_path or task.length <= 0:
        return None
    with open(task.file_path, "rb") as fh:
        fh.seek(task.offset)
        line = fh.read(task.length)
    return parse_session_bytes(task.project_dir_name, line, anonymizer)


def parse_sessions(project_dir_name: str, custom_dir: Path, anonymizer: Anonymizer) -> list[dict]:
    return list(
        parse_project_sessions(
            project_dir_name,
            anonymizer,
            include_thinking=True,
            custom_dir=custom_dir,
        )
    )


def parse_session_bytes(project_dir_name: str, raw_line: bytes | str, anonymizer: Anonymizer) -> dict | None:
    required_fields = {"session_id", "model", "messages"}

    if isinstance(raw_line, bytes):
        raw_line = raw_line.decode("utf-8", errors="replace")

    line = raw_line.strip()
    if not line:
        return None

    try:
        session = json.loads(line)
    except json.JSONDecodeError:
        return None

    if not isinstance(session, dict):
        return None

    missing = required_fields - session.keys()
    if missing:
        return None

    session["project"] = f"custom:{project_dir_name}"
    session["source"] = SOURCE
    for msg in session.get("messages", []):
        if "content" in msg and isinstance(msg["content"], str):
            redacted, _ = redact_text(msg["content"])
            msg["content"] = anonymizer.text(redacted)
    return session
