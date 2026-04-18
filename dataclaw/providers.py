from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from .anonymizer import Anonymizer
from .export_tasks import ExportSessionTask
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

    def discover_projects(self) -> list[dict]:
        raise NotImplementedError

    def parse_project_sessions(
        self,
        project_dir_name: str,
        anonymizer: Anonymizer,
        include_thinking: bool,
    ) -> Iterable[dict]:
        raise NotImplementedError

    def build_export_session_tasks(self, project_index: int, project: dict) -> list[ExportSessionTask]:
        raise NotImplementedError

    def parse_export_session_task(
        self,
        task: ExportSessionTask,
        anonymizer: Anonymizer,
        include_thinking: bool,
    ) -> dict | None:
        raise NotImplementedError

    def has_session_source(self) -> bool:
        return self.source_path.exists()

    def missing_source_message(self) -> str:
        return f"{self.source_path} was not found."


@dataclass(frozen=True)
class ModuleProvider(Provider):
    module: ModuleType

    @classmethod
    def from_module(cls, module: ModuleType, *, hf_metadata_tag: str, source_path_attr: str) -> ModuleProvider:
        return cls(
            source=module.SOURCE,
            hf_metadata_tag=hf_metadata_tag,
            source_path=getattr(module, source_path_attr),
            module=module,
        )

    def discover_projects(self) -> list[dict]:
        return self.module.discover_projects()

    def parse_project_sessions(
        self,
        project_dir_name: str,
        anonymizer: Anonymizer,
        include_thinking: bool,
    ) -> Iterable[dict]:
        return self.module.parse_project_sessions(project_dir_name, anonymizer, include_thinking)

    def build_export_session_tasks(self, project_index: int, project: dict) -> list[ExportSessionTask]:
        return self.module.build_export_session_tasks(project_index, project)

    def parse_export_session_task(
        self,
        task: ExportSessionTask,
        anonymizer: Anonymizer,
        include_thinking: bool,
    ) -> dict | None:
        return self.module.parse_export_session_task(task, anonymizer, include_thinking)


PROVIDERS: dict[str, Provider] = {
    _claude_mod.SOURCE: ModuleProvider.from_module(
        _claude_mod,
        hf_metadata_tag="claude-code",
        source_path_attr="CLAUDE_DIR",
    ),
    _codex_mod.SOURCE: ModuleProvider.from_module(
        _codex_mod,
        hf_metadata_tag="codex-cli",
        source_path_attr="CODEX_DIR",
    ),
    _cursor_mod.SOURCE: ModuleProvider.from_module(
        _cursor_mod,
        hf_metadata_tag="cursor",
        source_path_attr="CURSOR_DB",
    ),
    _custom_mod.SOURCE: ModuleProvider.from_module(
        _custom_mod,
        hf_metadata_tag="custom",
        source_path_attr="CUSTOM_DIR",
    ),
    _gemini_mod.SOURCE: ModuleProvider.from_module(
        _gemini_mod,
        hf_metadata_tag="gemini-cli",
        source_path_attr="GEMINI_DIR",
    ),
    _kimi_mod.SOURCE: ModuleProvider.from_module(
        _kimi_mod,
        hf_metadata_tag="kimi-cli",
        source_path_attr="KIMI_DIR",
    ),
    _opencode_mod.SOURCE: ModuleProvider.from_module(
        _opencode_mod,
        hf_metadata_tag="opencode",
        source_path_attr="OPENCODE_DIR",
    ),
    _openclaw_mod.SOURCE: ModuleProvider.from_module(
        _openclaw_mod,
        hf_metadata_tag="openclaw",
        source_path_attr="OPENCLAW_DIR",
    ),
}

PROVIDER_ORDER = tuple(PROVIDERS.values())


def get_provider(source: str) -> Provider:
    return PROVIDERS[source]


def iter_providers() -> tuple[Provider, ...]:
    return PROVIDER_ORDER
