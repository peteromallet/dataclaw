"""Public parser API for discovering and parsing provider sessions."""

from .anonymizer import Anonymizer
from .providers import PROVIDERS, iter_providers


def discover_projects() -> list[dict]:
    """Discover all supported source projects with session counts."""
    projects: list[dict] = []
    for provider in iter_providers():
        projects.extend(provider.discover_projects())
    return sorted(projects, key=lambda p: (p["display_name"], p["source"]))


def parse_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
    source: str = "claude",
) -> list[dict]:
    """Parse all sessions for a project into structured dicts."""
    provider = PROVIDERS.get(source, PROVIDERS["claude"])
    return provider.parse_project_sessions(project_dir_name, anonymizer, include_thinking)
