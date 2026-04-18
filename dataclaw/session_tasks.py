"""Generic export session task orchestration."""

from __future__ import annotations

from .anonymizer import Anonymizer
from .export_tasks import ExportSessionTask
from .providers import get_provider


def build_export_session_tasks(selected_projects: list[dict], default_source: str) -> list[ExportSessionTask]:
    tasks: list[ExportSessionTask] = []
    for project_index, project in enumerate(selected_projects):
        source = project.get("source", default_source)
        provider = get_provider(source)
        tasks.extend(provider.build_export_session_tasks(project_index, project))
    return tasks


def parse_export_session_task(
    task: ExportSessionTask,
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> dict | None:
    provider = get_provider(task.source)
    return provider.parse_export_session_task(task, anonymizer, include_thinking)
