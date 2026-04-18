"""Shared export task types."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExportSessionTask:
    source: str
    project_index: int
    task_index: int
    project_dir_name: str
    project_display_name: str
    estimated_bytes: int
    kind: str
    file_path: str | None = None
    item_id: str | None = None
    offset: int = 0
    length: int = 0
    default_model: str | None = None
