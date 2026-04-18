from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .anonymizer import Anonymizer
from .parsers import claude as _claude_mod
from .parsers import codex as _codex_mod
from .parsers import cursor as _cursor_mod
from .parsers import custom as _custom_mod
from .parsers import gemini as _gemini_mod
from .parsers import kimi as _kimi_mod
from .parsers import openclaw as _openclaw_mod
from .parsers import opencode as _opencode_mod


@dataclass(frozen=True)
class Provider:
    source: str
    hf_metadata_tag: str
    source_path: Path
    discover_projects: Callable[[], list[dict]]
    parse_project_sessions: Callable[[str, Anonymizer, bool], Iterable[dict]]

    def has_session_source(self) -> bool:
        return self.source_path.exists()

    def missing_source_message(self) -> str:
        return f"{self.source_path} was not found."


PROVIDERS: dict[str, Provider] = {
    _claude_mod.SOURCE: Provider(
        source=_claude_mod.SOURCE,
        hf_metadata_tag="claude-code",
        source_path=_claude_mod.CLAUDE_DIR,
        discover_projects=_claude_mod.discover_projects,
        parse_project_sessions=_claude_mod.parse_project_sessions,
    ),
    _codex_mod.SOURCE: Provider(
        source=_codex_mod.SOURCE,
        hf_metadata_tag="codex-cli",
        source_path=_codex_mod.CODEX_DIR,
        discover_projects=_codex_mod.discover_projects,
        parse_project_sessions=_codex_mod.parse_project_sessions,
    ),
    _cursor_mod.SOURCE: Provider(
        source=_cursor_mod.SOURCE,
        hf_metadata_tag="cursor",
        source_path=_cursor_mod.CURSOR_DB,
        discover_projects=_cursor_mod.discover_projects,
        parse_project_sessions=_cursor_mod.parse_project_sessions,
    ),
    _custom_mod.SOURCE: Provider(
        source=_custom_mod.SOURCE,
        hf_metadata_tag="custom",
        source_path=_custom_mod.CUSTOM_DIR,
        discover_projects=_custom_mod.discover_projects,
        parse_project_sessions=_custom_mod.parse_project_sessions,
    ),
    _gemini_mod.SOURCE: Provider(
        source=_gemini_mod.SOURCE,
        hf_metadata_tag="gemini-cli",
        source_path=_gemini_mod.GEMINI_DIR,
        discover_projects=_gemini_mod.discover_projects,
        parse_project_sessions=_gemini_mod.parse_project_sessions,
    ),
    _kimi_mod.SOURCE: Provider(
        source=_kimi_mod.SOURCE,
        hf_metadata_tag="kimi-cli",
        source_path=_kimi_mod.KIMI_DIR,
        discover_projects=_kimi_mod.discover_projects,
        parse_project_sessions=_kimi_mod.parse_project_sessions,
    ),
    _opencode_mod.SOURCE: Provider(
        source=_opencode_mod.SOURCE,
        hf_metadata_tag="opencode",
        source_path=_opencode_mod.OPENCODE_DIR,
        discover_projects=_opencode_mod.discover_projects,
        parse_project_sessions=_opencode_mod.parse_project_sessions,
    ),
    _openclaw_mod.SOURCE: Provider(
        source=_openclaw_mod.SOURCE,
        hf_metadata_tag="openclaw",
        source_path=_openclaw_mod.OPENCLAW_DIR,
        discover_projects=_openclaw_mod.discover_projects,
        parse_project_sessions=_openclaw_mod.parse_project_sessions,
    ),
}

PROVIDER_ORDER = tuple(PROVIDERS.values())


def get_provider(source: str) -> Provider:
    return PROVIDERS[source]


def iter_providers() -> tuple[Provider, ...]:
    return PROVIDER_ORDER
