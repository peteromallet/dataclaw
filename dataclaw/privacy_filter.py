"""Optional privacy-filter PII scanning with lazy model imports."""

from dataclasses import dataclass, replace
import hashlib
import json
import logging
import re
from collections.abc import Callable
from typing import Any, Iterable

MODEL_ID = "openai/privacy-filter"
_DEFAULT_MIN_SCORE = 0.85
_CHUNK_TOKENS = 480
_TEXT_PROGRESS_MIN_CHARS = 20_000
_TEXT_PROGRESS_CHUNK_INTERVAL = 25
_MAX_MODEL_SESSION_CHARS = 25_000_000
_MAX_MODEL_SESSION_STRINGS = 40_000

_LOG = logging.getLogger(__name__)
_PIPELINES: dict[tuple[str, str | None, str], Any] = {}
_TOKEN_RE = re.compile(r"\S+")
ProgressCallback = Callable[[str, dict[str, Any]], None]

# Mapping from string dtype config values to torch dtypes. Resolved lazily so
# that importing this module never imports torch.
_DTYPE_ALIASES = {"auto", "fp32", "float32", "bf16", "bfloat16", "fp16", "float16"}


@dataclass(frozen=True)
class Finding:
    entity: str
    text: str
    score: float
    start: int | None = None
    end: int | None = None
    field: str | None = None
    session_id: str | None = None
    source: str | None = None

    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.entity}|{self.text}".encode()).hexdigest()


def is_available() -> bool:
    try:
        import transformers  # noqa: F401
        import torch  # noqa: F401
    except Exception:
        return False
    return True


def _config_dtype() -> str:
    """Read ``config.privacy_filter.dtype`` lazily; default to ``"auto"``."""
    try:
        from . import config as _config_mod
        cfg = _config_mod.load_config()
    except Exception:  # pragma: no cover - config IO is best-effort
        return "auto"
    pf_cfg = cfg.get("privacy_filter") if isinstance(cfg, dict) else None
    if isinstance(pf_cfg, dict):
        value = pf_cfg.get("dtype")
        if isinstance(value, str) and value.lower() in _DTYPE_ALIASES:
            return value.lower()
    return "auto"


def _config_device() -> str | None:
    """Read ``config.privacy_filter.device`` lazily."""
    try:
        from . import config as _config_mod
        cfg = _config_mod.load_config()
    except Exception:  # pragma: no cover - config IO is best-effort
        return None
    pf_cfg = cfg.get("privacy_filter") if isinstance(cfg, dict) else None
    if isinstance(pf_cfg, dict):
        value = pf_cfg.get("device")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _auto_device() -> str:
    try:
        import torch

        if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def resolve_device(device: str | None = None) -> str:
    value = device if device is not None else _config_device()
    if not isinstance(value, str) or not value.strip() or value.strip().lower() == "auto":
        return _auto_device()
    return value.strip()


def _resolve_dtype(name: str, device: str | None = None) -> Any:
    """Translate a dtype name to a ``torch.dtype``, applying ``auto`` rules.

    - ``auto`` -> ``bfloat16`` only when the configured device is MPS, else
      ``float32``. CPU is the default for predictable unattended runs.
    - ``fp32``/``float32`` -> ``torch.float32``
    - ``bf16``/``bfloat16`` -> ``torch.bfloat16``
    - ``fp16``/``float16`` -> ``torch.float16``
    """
    import torch

    key = (name or "auto").lower()
    if key == "auto":
        return torch.bfloat16 if str(device or "").lower().startswith("mps") else torch.float32
    if key in ("bf16", "bfloat16"):
        return torch.bfloat16
    if key in ("fp16", "float16"):
        return torch.float16
    return torch.float32


def _build_pipeline(**kwargs: Any) -> Any:
    """Indirection so tests can patch pipeline construction without monkey-
    patching the (lazy) ``transformers`` module itself."""
    from transformers import pipeline
    return pipeline(**kwargs)


def _load(
    *,
    device: str | None = None,
    min_score: float = _DEFAULT_MIN_SCORE,
    dtype: str | None = None,
) -> Any:
    del min_score
    effective_device = resolve_device(device)
    dtype_name = (dtype or _config_dtype()).lower()
    key = (MODEL_ID, effective_device, dtype_name)
    if key not in _PIPELINES:
        torch_dtype = _resolve_dtype(dtype_name, effective_device)
        _LOG.info(
            "privacy_filter_model_load_started",
            extra={
                "phase": "privacy_filter",
                "extra": {"model": MODEL_ID, "device": effective_device, "dtype": dtype_name},
            },
        )
        # transformers >=4.45 deprecated ``torch_dtype`` in favour of ``dtype``;
        # we pin >=4.57 in pyproject.toml so the new name is always available.
        kwargs: dict[str, Any] = {
            "task": "token-classification",
            "model": MODEL_ID,
            "aggregation_strategy": "simple",
            "dtype": torch_dtype,
        }
        if effective_device is not None:
            kwargs["device"] = effective_device
        _PIPELINES[key] = _build_pipeline(**kwargs)
        _LOG.info(
            "privacy_filter_model_load_finished",
            extra={
                "phase": "privacy_filter",
                "extra": {"model": MODEL_ID, "device": effective_device, "dtype": dtype_name},
            },
        )
    return _PIPELINES[key]


def _chunk_by_tokens(text: str, max_tokens: int = _CHUNK_TOKENS) -> Iterable[tuple[str, int]]:
    group: list[re.Match[str]] = []
    saw_match = False
    for match in _TOKEN_RE.finditer(text):
        saw_match = True
        group.append(match)
        if len(group) >= max_tokens:
            yield text[group[0].start():group[-1].end()], group[0].start()
            group.clear()
    if group:
        yield text[group[0].start():group[-1].end()], group[0].start()
    if not saw_match and text:
        yield text, 0


def _entity_name(raw: dict[str, Any]) -> str:
    value = raw.get("entity_group") or raw.get("entity") or raw.get("label")
    return str(value or "PII")


def _entity_text(raw: dict[str, Any], chunk: str) -> str:
    word = raw.get("word") or raw.get("text")
    if isinstance(word, str) and word.strip():
        return word.strip()
    start = raw.get("start")
    end = raw.get("end")
    if isinstance(start, int) and isinstance(end, int):
        return chunk[start:end]
    return ""


def scan_text(
    text: str,
    *,
    device: str | None = None,
    min_score: float = _DEFAULT_MIN_SCORE,
    progress_callback: ProgressCallback | None = None,
    field: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
) -> list[Finding]:
    pipe = _load(device=device, min_score=min_score)
    findings: list[Finding] = []
    should_log = progress_callback is not None and len(text) >= _TEXT_PROGRESS_MIN_CHARS
    if should_log:
        progress_callback("privacy_filter_text_started", {
            "field": field,
            "session_id": session_id,
            "source": source,
            "char_count": len(text),
            "chunk_count": None,
        })
    chunk_index = 0
    for chunk_index, (chunk, offset) in enumerate(_chunk_by_tokens(text), start=1):
        for raw in pipe(chunk):
            if not isinstance(raw, dict):
                continue
            score = float(raw.get("score") or 0.0)
            if score < min_score:
                continue
            start = raw.get("start")
            end = raw.get("end")
            findings.append(Finding(
                entity=_entity_name(raw),
                text=_entity_text(raw, chunk),
                score=score,
                start=offset + start if isinstance(start, int) else None,
                end=offset + end if isinstance(end, int) else None,
            ))
        if should_log and (
            chunk_index == 1
            or chunk_index % _TEXT_PROGRESS_CHUNK_INTERVAL == 0
        ):
            progress_callback("privacy_filter_text_progress", {
                "field": field,
                "session_id": session_id,
                "source": source,
                "char_count": len(text),
                "chunk_index": chunk_index,
                "chunk_count": None,
                "findings": len(findings),
            })
    if should_log:
        progress_callback("privacy_filter_text_finished", {
            "field": field,
            "session_id": session_id,
            "source": source,
            "char_count": len(text),
            "chunk_count": chunk_index,
            "findings": len(findings),
        })
    return findings


def _walk_strings(value: Any, prefix: str) -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_strings(child, f"{prefix}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_strings(child, f"{prefix}[{index}]")


def _session_id(session: dict[str, Any]) -> str | None:
    for key in ("id", "session_id", "conversation_id"):
        value = session.get(key)
        if isinstance(value, str):
            return value
    return None


def scan_session(
    session: dict[str, Any],
    *,
    device: str | None = None,
    min_score: float = _DEFAULT_MIN_SCORE,
    progress_callback: ProgressCallback | None = None,
    include_tool_io: bool = True,
    roles: set[str] | None = None,
) -> list[Finding]:
    found: list[Finding] = []
    session_id = _session_id(session)
    source = session.get("source") if isinstance(session.get("source"), str) else None

    for msg_index, msg in enumerate(session.get("messages", [])):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if roles is not None and (not isinstance(role, str) or role not in roles):
            continue
        for field in ("content", "thinking"):
            if field in msg:
                prefix = f"messages[{msg_index}].{field}"
                for path, text in _walk_strings(msg[field], prefix):
                    for finding in scan_text(
                        text,
                        device=device,
                        min_score=min_score,
                        progress_callback=progress_callback,
                        field=path,
                        session_id=session_id,
                        source=source,
                    ):
                        found.append(_with_context(finding, path, session_id, source))
        if include_tool_io:
            for tool_index, tool_use in enumerate(msg.get("tool_uses", [])):
                if not isinstance(tool_use, dict):
                    continue
                for field in ("input", "output"):
                    if field in tool_use:
                        prefix = f"messages[{msg_index}].tool_uses[{tool_index}].{field}"
                        for path, text in _walk_strings(tool_use[field], prefix):
                            for finding in scan_text(
                                text,
                                device=device,
                                min_score=min_score,
                                progress_callback=progress_callback,
                                field=path,
                                session_id=session_id,
                                source=source,
                            ):
                                found.append(_with_context(finding, path, session_id, source))
    return found


def redact_text(
    text: str,
    *,
    device: str | None = None,
    min_score: float = _DEFAULT_MIN_SCORE,
    progress_callback: ProgressCallback | None = None,
    field: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
) -> tuple[str, list[Finding]]:
    findings = scan_text(
        text,
        device=device,
        min_score=min_score,
        progress_callback=progress_callback,
        field=field,
        session_id=session_id,
        source=source,
    )
    positioned = [f for f in findings if isinstance(f.start, int) and isinstance(f.end, int) and f.end > f.start]
    if not positioned:
        return text, findings
    redacted = text
    for finding in sorted(positioned, key=lambda f: int(f.start or 0), reverse=True):
        start = int(finding.start or 0)
        end = int(finding.end or start)
        redacted = redacted[:start] + "[REDACTED]" + redacted[end:]
    return redacted, findings


def _redact_strings_in_value(
    value: Any,
    prefix: str,
    *,
    device: str | None,
    min_score: float,
    progress_callback: ProgressCallback | None,
    session_id: str | None,
    source: str | None,
) -> tuple[Any, list[Finding]]:
    if isinstance(value, str):
        redacted, findings = redact_text(
            value,
            device=device,
            min_score=min_score,
            progress_callback=progress_callback,
            field=prefix,
            session_id=session_id,
            source=source,
        )
        return redacted, [_with_context(f, prefix, session_id, source) for f in findings]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        findings: list[Finding] = []
        for key, child in value.items():
            redacted, child_findings = _redact_strings_in_value(
                child,
                f"{prefix}.{key}",
                device=device,
                min_score=min_score,
                progress_callback=progress_callback,
                session_id=session_id,
                source=source,
            )
            out[key] = redacted
            findings.extend(child_findings)
        return out, findings
    if isinstance(value, list):
        out_list: list[Any] = []
        findings: list[Finding] = []
        for index, child in enumerate(value):
            redacted, child_findings = _redact_strings_in_value(
                child,
                f"{prefix}[{index}]",
                device=device,
                min_score=min_score,
                progress_callback=progress_callback,
                session_id=session_id,
                source=source,
            )
            out_list.append(redacted)
            findings.extend(child_findings)
        return out_list, findings
    return value, []


def _replace_strings(value: Any, replacement: str) -> Any:
    if isinstance(value, str):
        return replacement if value else value
    if isinstance(value, dict):
        return {key: _replace_strings(child, replacement) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_strings(child, replacement) for child in value]
    return value


def _redact_oversized_session(
    session: dict[str, Any],
    *,
    include_tool_io: bool,
    roles: set[str] | None,
    reason: str,
) -> tuple[dict[str, Any], list[Finding]]:
    redacted_session = session
    session_id = _session_id(redacted_session)
    source = redacted_session.get("source") if isinstance(redacted_session.get("source"), str) else None
    replacement = "[REDACTED: oversized session]"
    redacted_fields = 0
    for msg in redacted_session.get("messages", []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if roles is not None and (not isinstance(role, str) or role not in roles):
            continue
        for field in ("content", "thinking"):
            if field in msg:
                msg[field] = _replace_strings(msg[field], replacement)
                redacted_fields += 1
        if include_tool_io:
            for tool_use in msg.get("tool_uses", []):
                if not isinstance(tool_use, dict):
                    continue
                for field in ("input", "output"):
                    if field in tool_use:
                        tool_use[field] = _replace_strings(tool_use[field], replacement)
                        redacted_fields += 1
    finding = Finding(
        entity="OVERSIZED_SESSION_REDACTED",
        text=reason,
        score=1.0,
        field=f"messages ({redacted_fields} fields)",
        session_id=session_id,
        source=source,
    )
    return redacted_session, [finding]


def redact_session(
    session: dict[str, Any],
    *,
    device: str | None = None,
    min_score: float = _DEFAULT_MIN_SCORE,
    progress_callback: ProgressCallback | None = None,
    include_tool_io: bool = True,
    roles: set[str] | None = None,
) -> tuple[dict[str, Any], list[Finding]]:
    redacted_session = json.loads(json.dumps(session))
    found: list[Finding] = []
    session_id = _session_id(redacted_session)
    source = redacted_session.get("source") if isinstance(redacted_session.get("source"), str) else None

    for msg_index, msg in enumerate(redacted_session.get("messages", [])):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if roles is not None and (not isinstance(role, str) or role not in roles):
            continue
        for field in ("content", "thinking"):
            if field in msg:
                prefix = f"messages[{msg_index}].{field}"
                msg[field], findings = _redact_strings_in_value(
                    msg[field],
                    prefix,
                    device=device,
                    min_score=min_score,
                    progress_callback=progress_callback,
                    session_id=session_id,
                    source=source,
                )
                found.extend(findings)
        if include_tool_io:
            for tool_index, tool_use in enumerate(msg.get("tool_uses", [])):
                if not isinstance(tool_use, dict):
                    continue
                for field in ("input", "output"):
                    if field in tool_use:
                        prefix = f"messages[{msg_index}].tool_uses[{tool_index}].{field}"
                        tool_use[field], findings = _redact_strings_in_value(
                            tool_use[field],
                            prefix,
                            device=device,
                            min_score=min_score,
                            progress_callback=progress_callback,
                            session_id=session_id,
                            source=source,
                        )
                        found.extend(findings)
    return redacted_session, found


def _with_context(finding: Finding, field: str, session_id: str | None, source: str | None) -> Finding:
    return replace(finding, field=field, session_id=session_id, source=source)


def _string_stats(value: Any) -> tuple[int, int]:
    string_count = 0
    char_count = 0
    for _, text in _walk_strings(value, ""):
        string_count += 1
        char_count += len(text)
    return string_count, char_count


def scan_jsonl(
    path: Any,
    *,
    device: str | None = None,
    min_score: float = _DEFAULT_MIN_SCORE,
    progress_callback: ProgressCallback | None = None,
    shard_index: int | None = None,
    total_shards: int | None = None,
    include_tool_io: bool = True,
    roles: set[str] | None = None,
) -> list[Finding]:
    from pathlib import Path

    findings: list[Finding] = []
    session_count = 0
    path_obj = Path(path)
    if progress_callback is not None:
        progress_callback("privacy_filter_shard_started", {
            "path": str(path_obj),
            "shard_index": shard_index,
            "total_shards": total_shards,
        })
    with path_obj.open(errors="replace") as f:
        for line_no, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                session = json.loads(line)
            except json.JSONDecodeError as exc:
                _LOG.warning("Skipping invalid JSONL line %s in %s: %s", line_no, path, exc)
                continue
            if isinstance(session, dict):
                session_count += 1
                before = len(findings)
                session_id = _session_id(session)
                source = session.get("source") if isinstance(session.get("source"), str) else None
                string_count, char_count = _string_stats(session.get("messages", []))
                if progress_callback is not None:
                    progress_callback("privacy_filter_session_started", {
                        "path": str(path_obj),
                        "shard_index": shard_index,
                        "total_shards": total_shards,
                        "line_no": line_no,
                        "sessions_scanned": session_count,
                        "session_id": session_id,
                        "source": source,
                        "message_count": len(session.get("messages", [])) if isinstance(session.get("messages"), list) else None,
                        "string_count": string_count,
                        "char_count": char_count,
                    })
                findings.extend(scan_session(
                    session,
                    device=device,
                    min_score=min_score,
                    progress_callback=progress_callback,
                    include_tool_io=include_tool_io,
                    roles=roles,
                ))
                if progress_callback is not None:
                    progress_callback("privacy_filter_session_finished", {
                        "path": str(path_obj),
                        "shard_index": shard_index,
                        "total_shards": total_shards,
                        "line_no": line_no,
                        "sessions_scanned": session_count,
                        "session_id": session_id,
                        "source": source,
                        "findings": len(findings),
                        "session_findings": len(findings) - before,
                    })
                if progress_callback is not None and (session_count == 1 or session_count % 100 == 0):
                    progress_callback("privacy_filter_shard_progress", {
                        "path": str(path_obj),
                        "shard_index": shard_index,
                        "total_shards": total_shards,
                        "sessions_scanned": session_count,
                        "findings": len(findings),
                    })
    if progress_callback is not None:
        progress_callback("privacy_filter_shard_finished", {
            "path": str(path_obj),
            "shard_index": shard_index,
            "total_shards": total_shards,
            "sessions_scanned": session_count,
            "findings": len(findings),
        })
    return findings


def _iter_manifest_shard_paths(run_dir: Any, manifest: dict[str, Any]) -> list[Any]:
    from pathlib import Path

    root = Path(run_dir)
    return [root / s["path"] for s in manifest.get("shards", [])
            if isinstance(s, dict) and isinstance(s.get("path"), str)]


def scan_shards(
    run_dir: Any,
    manifest: dict[str, Any],
    *,
    device: str | None = None,
    min_score: float = _DEFAULT_MIN_SCORE,
    progress_callback: ProgressCallback | None = None,
    include_tool_io: bool = True,
    roles: set[str] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    shard_paths = _iter_manifest_shard_paths(run_dir, manifest)
    total_shards = len(shard_paths)
    for shard_index, shard_path in enumerate(shard_paths, start=1):
        findings.extend(scan_jsonl(
            shard_path,
            device=device,
            min_score=min_score,
            progress_callback=progress_callback,
            shard_index=shard_index,
            total_shards=total_shards,
            include_tool_io=include_tool_io,
            roles=roles,
        ))
    return findings


def redact_jsonl(
    path: Any,
    *,
    device: str | None = None,
    min_score: float = _DEFAULT_MIN_SCORE,
    progress_callback: ProgressCallback | None = None,
    shard_index: int | None = None,
    total_shards: int | None = None,
    include_tool_io: bool = True,
    roles: set[str] | None = None,
) -> list[Finding]:
    from pathlib import Path

    findings: list[Finding] = []
    session_count = 0
    path_obj = Path(path)
    tmp_path = path_obj.with_suffix(path_obj.suffix + ".tmp")
    if progress_callback is not None:
        progress_callback("privacy_filter_shard_started", {
            "path": str(path_obj),
            "shard_index": shard_index,
            "total_shards": total_shards,
        })
    with path_obj.open(errors="replace") as src, tmp_path.open("w", encoding="utf-8") as dst:
        for line_no, line in enumerate(src, start=1):
            if not line.strip():
                dst.write(line)
                continue
            try:
                session = json.loads(line)
            except json.JSONDecodeError as exc:
                _LOG.warning("Skipping invalid JSONL line %s in %s: %s", line_no, path, exc)
                dst.write(line)
                continue
            if not isinstance(session, dict):
                dst.write(line)
                continue
            session_count += 1
            before = len(findings)
            session_id = _session_id(session)
            source = session.get("source") if isinstance(session.get("source"), str) else None
            string_count, char_count = _string_stats(session.get("messages", []))
            if progress_callback is not None:
                progress_callback("privacy_filter_session_started", {
                    "path": str(path_obj),
                    "shard_index": shard_index,
                    "total_shards": total_shards,
                    "line_no": line_no,
                    "sessions_scanned": session_count,
                    "session_id": session_id,
                    "source": source,
                    "message_count": len(session.get("messages", [])) if isinstance(session.get("messages"), list) else None,
                    "string_count": string_count,
                    "char_count": char_count,
                })
            if char_count > _MAX_MODEL_SESSION_CHARS or string_count > _MAX_MODEL_SESSION_STRINGS:
                reason = (
                    f"session exceeds model privacy-filter guard "
                    f"({char_count} chars, {string_count} strings)"
                )
                if progress_callback is not None:
                    progress_callback("privacy_filter_session_size_guard_redacted", {
                        "path": str(path_obj),
                        "shard_index": shard_index,
                        "total_shards": total_shards,
                        "line_no": line_no,
                        "sessions_scanned": session_count,
                        "session_id": session_id,
                        "source": source,
                        "message_count": len(session.get("messages", [])) if isinstance(session.get("messages"), list) else None,
                        "string_count": string_count,
                        "char_count": char_count,
                        "max_chars": _MAX_MODEL_SESSION_CHARS,
                        "max_strings": _MAX_MODEL_SESSION_STRINGS,
                    })
                redacted_session, session_findings = _redact_oversized_session(
                    session,
                    include_tool_io=include_tool_io,
                    roles=roles,
                    reason=reason,
                )
            else:
                redacted_session, session_findings = redact_session(
                    session,
                    device=device,
                    min_score=min_score,
                    progress_callback=progress_callback,
                    include_tool_io=include_tool_io,
                    roles=roles,
                )
            findings.extend(session_findings)
            dst.write(json.dumps(redacted_session, ensure_ascii=False) + "\n")
            if progress_callback is not None:
                progress_callback("privacy_filter_session_finished", {
                    "path": str(path_obj),
                    "shard_index": shard_index,
                    "total_shards": total_shards,
                    "line_no": line_no,
                    "sessions_scanned": session_count,
                    "session_id": session_id,
                    "source": source,
                    "findings": len(findings),
                    "session_findings": len(findings) - before,
                })
            if progress_callback is not None and (session_count == 1 or session_count % 100 == 0):
                progress_callback("privacy_filter_shard_progress", {
                    "path": str(path_obj),
                    "shard_index": shard_index,
                    "total_shards": total_shards,
                    "sessions_scanned": session_count,
                    "findings": len(findings),
                })
    tmp_path.replace(path_obj)
    if progress_callback is not None:
        progress_callback("privacy_filter_shard_finished", {
            "path": str(path_obj),
            "shard_index": shard_index,
            "total_shards": total_shards,
            "sessions_scanned": session_count,
            "findings": len(findings),
        })
    return findings


def redact_shards(
    run_dir: Any,
    manifest: dict[str, Any],
    *,
    device: str | None = None,
    min_score: float = _DEFAULT_MIN_SCORE,
    progress_callback: ProgressCallback | None = None,
    include_tool_io: bool = True,
    roles: set[str] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    shard_paths = _iter_manifest_shard_paths(run_dir, manifest)
    total_shards = len(shard_paths)
    for shard_index, shard_path in enumerate(shard_paths, start=1):
        findings.extend(redact_jsonl(
            shard_path,
            device=device,
            min_score=min_score,
            progress_callback=progress_callback,
            shard_index=shard_index,
            total_shards=total_shards,
            include_tool_io=include_tool_io,
            roles=roles,
        ))
    return findings


def diff_findings(
    findings: Iterable[Finding],
    known_findings: dict[str, Any] | None,
) -> tuple[list[Finding], list[Finding]]:
    known = set((known_findings or {}).keys())
    new: list[Finding] = []
    old: list[Finding] = []
    for finding in findings:
        if finding.fingerprint() in known:
            old.append(finding)
        else:
            new.append(finding)
    return new, old


def record_findings(
    findings: Iterable[Finding],
    known_findings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    registry = dict(known_findings or {})
    now = datetime.now(tz=timezone.utc).isoformat()
    for finding in findings:
        fp = finding.fingerprint()
        existing = registry.get(fp) if isinstance(registry.get(fp), dict) else {}
        first_seen = existing.get("first_seen") or now
        registry[fp] = {
            "entity": finding.entity,
            "text": finding.text,
            "first_seen": first_seen,
            "last_seen": now,
            "count": int(existing.get("count", 0)) + 1,
        }
    return registry
