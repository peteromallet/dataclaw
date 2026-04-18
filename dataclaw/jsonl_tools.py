"""Utilities for rendering and diffing DataClaw JSONL exports."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter, deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson
import yaml

from ._workers import configured_workers
from .secrets import contains_large_binary_value, should_skip_large_binary_string

IDENTITY_FIELDS = ("source", "project", "session_id", "start_time")
OMITTED_ORIGINAL_FILE = "<omitted originalFile content>"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
DEFAULT_YAML_SUFFIX = "_formatted.yaml"
DEFAULT_DIFF_SUFFIX = "_diff.yaml"
_LARGE_BLOB_MARKER_RE = re.compile(r"^__DATACLAW_LARGE_BLOB__:(\d+):[0-9a-f]{16}$")
_YAML_WIDTH = 2147483647
_BaseDumper = getattr(yaml, "CDumper", yaml.SafeDumper)


class Dumper(_BaseDumper):
    pass


def _str_representer(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


Dumper.add_representer(str, _str_representer)


def encode_emojis(text: str) -> str:
    return "".join(f"__EMOJI_{ord(char):x}__" if ord(char) > 0xFFFF else char for char in text)


def _large_blob_marker(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"__DATACLAW_LARGE_BLOB__:{len(text)}:{digest}"


def _large_blob_summary_from_marker(text: str) -> dict[str, Any] | None:
    match = _LARGE_BLOB_MARKER_RE.fullmatch(text)
    if match is None:
        return None
    return {"type": "large_blob", "length": int(match.group(1))}


def prepare_large_binary_diff_value(value: Any) -> Any:
    if isinstance(value, str):
        return _large_blob_marker(value) if should_skip_large_binary_string(value) else value
    if isinstance(value, dict):
        return {key: prepare_large_binary_diff_value(child_value) for key, child_value in value.items()}
    if isinstance(value, list):
        return [prepare_large_binary_diff_value(item) for item in value]
    return value


def contains_large_binary_marker(value: Any) -> bool:
    if isinstance(value, str):
        return _large_blob_summary_from_marker(value) is not None
    if isinstance(value, dict):
        return any(contains_large_binary_marker(child_value) for child_value in value.values())
    if isinstance(value, list):
        return any(contains_large_binary_marker(item) for item in value)
    return False


def summarize_large_binary_markers(value: Any) -> Any:
    if isinstance(value, str):
        summary = _large_blob_summary_from_marker(value)
        return summary if summary is not None else value
    if isinstance(value, dict):
        return {key: summarize_large_binary_markers(child_value) for key, child_value in value.items()}
    if isinstance(value, list):
        return [summarize_large_binary_markers(item) for item in value]
    return value


def summarize_large_binary_patch_ops(patch_ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summarized = []
    for op in patch_ops:
        had_large_blob_marker = any(
            contains_large_binary_marker(op.get(field)) for field in ("value", "old", "new") if field in op
        )
        summarized_op = summarize_large_binary_markers(op)
        if summarized_op.get("op") == "replace" and had_large_blob_marker:
            summarized_op["op"] = "replace_large_blob"
        summarized.append(summarized_op)
    return summarized


class DecodeStream:
    def __init__(self, stream):
        self.stream = stream
        self.pattern = re.compile(r"__EMOJI_([0-9a-fA-F]+)__")

    def write(self, text: str) -> None:
        def repl(match):
            return chr(int(match.group(1), 16))

        self.stream.write(self.pattern.sub(repl, text))

    def flush(self) -> None:
        self.stream.flush()


@dataclass
class FileIndex:
    path: Path
    total_records: int
    groups: dict[tuple[Any, ...], dict[str, Any]]


@dataclass
class DiffResult:
    output_path: Path
    event_count: int
    summary: dict[str, int]


def clean_strings(obj: Any) -> Any:
    if isinstance(obj, str):
        text = ANSI_RE.sub("", obj)
        text = text.replace("\t", "    ")
        text = "\n".join(line.rstrip() for line in text.split("\n"))
        return encode_emojis(text)
    if isinstance(obj, dict):
        return {key: clean_strings(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [clean_strings(value) for value in obj]
    return obj


def default_yaml_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}{DEFAULT_YAML_SUFFIX}")


def default_diff_output_path(new_path: Path) -> Path:
    return new_path.with_name(f"{new_path.stem}{DEFAULT_DIFF_SUFFIX}")


def _open_text_output(path: Path):
    return path.open("w", encoding="utf-8", newline="\n")


def _resolve_diff_workers(task_count: int, workers: int | None = None) -> int:
    if task_count < 2:
        return 1

    if workers is None:
        workers = configured_workers()

    if workers is None:
        workers = os.cpu_count() or 1

    return max(1, min(workers, task_count))


def yaml_dump_documents(documents: Iterable[dict[str, Any]], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with _open_text_output(output_path) as handle:
        decoded_handle = DecodeStream(handle)
        for document in documents:
            _yaml_dump_document(document, handle, decoded_handle)
    return output_path


def _yaml_dump_document(document: dict[str, Any], handle, decoded_handle: DecodeStream) -> None:
    handle.write("---\n")
    yaml.dump(
        clean_strings(document),
        decoded_handle,
        Dumper=Dumper,
        default_flow_style=False,
        allow_unicode=True,
        width=_YAML_WIDTH,
        sort_keys=False,
    )


def jsonl_to_yaml_file(input_path: Path, output_path: Path | None = None) -> Path:
    if output_path is None:
        output_path = default_yaml_output_path(input_path)

    def iter_documents() -> Iterable[dict[str, Any]]:
        with input_path.open("rb") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                yield orjson.loads(line)

    return yaml_dump_documents(iter_documents(), output_path)


def canonical_record_bytes(obj: Any) -> bytes:
    return orjson.dumps(obj, option=orjson.OPT_SORT_KEYS)


def record_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_record_bytes(obj)).hexdigest()


def identity_key(obj: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(obj.get(field) for field in IDENTITY_FIELDS)


def identity_dict(key: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(IDENTITY_FIELDS, key, strict=True))


def normalize_for_diff(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {child_key: normalize_for_diff(child_value, child_key) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [normalize_for_diff(item, key) for item in value]
    if key == "originalFile" and isinstance(value, str):
        return OMITTED_ORIGINAL_FILE
    return value


def index_jsonl(path: Path) -> FileIndex:
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    total_records = 0
    with path.open("rb") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            obj = normalize_for_diff(orjson.loads(line))
            total_records += 1
            key = identity_key(obj)
            digest = record_hash(obj)
            group = groups.get(key)
            if group is None:
                group = {"first_line": line_number, "counts": Counter()}
                groups[key] = group
            group["counts"][digest] += 1
    return FileIndex(path=path, total_records=total_records, groups=groups)


def order_keys(old_index: FileIndex, new_index: FileIndex) -> list[tuple[Any, ...]]:
    all_keys = set(old_index.groups) | set(new_index.groups)

    def sort_key(key: tuple[Any, ...]) -> tuple[int, int]:
        if key in new_index.groups:
            return (0, new_index.groups[key]["first_line"])
        return (1, old_index.groups[key]["first_line"])

    return sorted(all_keys, key=sort_key)


def collect_changed_keys(old_index: FileIndex, new_index: FileIndex) -> list[tuple[Any, ...]]:
    changed = []
    for key in order_keys(old_index, new_index):
        old_counts = old_index.groups.get(key, {}).get("counts", Counter())
        new_counts = new_index.groups.get(key, {}).get("counts", Counter())
        if old_counts != new_counts:
            changed.append(key)
    return changed


def load_records_for_keys(path: Path, keys: set[tuple[Any, ...]]) -> dict[tuple[Any, ...], dict[str, Any]]:
    if not keys:
        return {}

    records: dict[tuple[Any, ...], dict[str, Any]] = {}
    with path.open("rb") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            obj = normalize_for_diff(orjson.loads(line))
            key = identity_key(obj)
            if key not in keys:
                continue
            digest = record_hash(obj)
            group = records.setdefault(key, {})
            entry = group.get(digest)
            if entry is None:
                entry = {"obj": obj, "line_numbers": deque()}
                group[digest] = entry
            entry["line_numbers"].append(line_number)
    return records


def iter_expanded_hashes(counter: Counter[str]) -> Iterable[str]:
    for digest in sorted(counter):
        for _ in range(counter[digest]):
            yield digest


def _pop_line_number(entry: dict[str, Any]) -> int:
    return entry["line_numbers"].popleft()


def _take_line_numbers(entry: dict[str, Any], count: int) -> list[int]:
    return [_pop_line_number(entry) for _ in range(count)]


def join_json_pointer(path_prefix: str, child_path: str) -> str:
    if not path_prefix:
        return child_path
    if not child_path:
        return path_prefix
    if child_path.startswith("/"):
        return f"{path_prefix}{child_path}"
    return f"{path_prefix}/{child_path}"


def exact_match_key_for_array_item(value: Any) -> tuple[Any, ...] | None:
    if not isinstance(value, dict):
        return None

    if "role" in value and "timestamp" in value:
        tools = []
        for tool_use in value.get("tool_uses", []):
            if isinstance(tool_use, dict):
                tools.append(tool_use.get("tool"))
        content = value.get("content") if isinstance(value.get("content"), str) else None
        return ("message", value.get("role"), value.get("timestamp"), tuple(tools), content)

    if "tool" in value and isinstance(value.get("input"), dict):
        return ("tool_use", value.get("tool"), record_hash(value.get("input")))

    return None


def loose_match_key_for_array_item(value: Any) -> tuple[Any, ...] | None:
    if not isinstance(value, dict):
        return None

    if "role" in value and "timestamp" in value:
        tools = []
        for tool_use in value.get("tool_uses", []):
            if isinstance(tool_use, dict):
                tools.append(tool_use.get("tool"))
        return (
            "message",
            value.get("role"),
            value.get("timestamp"),
            tuple(tools),
            "thinking" in value,
            "content_parts" in value,
        )

    if "tool" in value and isinstance(value.get("input"), dict):
        return ("tool_use", value.get("tool"), tuple(sorted(value.get("input", {}))))

    return None


def _pair_array_item_ops(
    remove_ops: list[dict[str, Any]],
    add_ops: list[dict[str, Any]],
    key_fn,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]] | None:
    add_buckets: dict[tuple[Any, ...], deque[dict[str, Any]]] = {}
    add_keys: list[tuple[Any, ...]] = []
    for op in add_ops:
        key = key_fn(op.get("value"))
        if key is None:
            return None
        add_buckets.setdefault(key, deque()).append(op)
        add_keys.append(key)

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    paired_remove_ids: set[int] = set()
    paired_add_ids: set[int] = set()

    for op in remove_ops:
        key = key_fn(op.get("value"))
        if key is None:
            return None
        add_queue = add_buckets.get(key)
        if not add_queue:
            continue
        add_op = add_queue.popleft()
        pairs.append((op, add_op))
        paired_remove_ids.add(id(op))
        paired_add_ids.add(id(add_op))

    remaining_removes = [op for op in remove_ops if id(op) not in paired_remove_ids]
    remaining_adds = [op for op in add_ops if id(op) not in paired_add_ids]
    return pairs, remaining_removes, remaining_adds


def expand_array_item_run(ops: list[dict[str, Any]], path_prefix: str) -> list[dict[str, Any]] | None:
    if not ops:
        return []
    path = ops[0].get("path")
    if not path or any(op.get("path") != path or op.get("op") not in {"remove", "add"} for op in ops):
        return None

    removes = [op for op in ops if op.get("op") == "remove"]
    adds = [op for op in ops if op.get("op") == "add"]
    if not removes or not adds:
        return None

    exact_pairs_result = _pair_array_item_ops(removes, adds, exact_match_key_for_array_item)
    if exact_pairs_result is None:
        return None

    exact_pairs, remaining_removes, remaining_adds = exact_pairs_result
    loose_pairs_result = _pair_array_item_ops(remaining_removes, remaining_adds, loose_match_key_for_array_item)
    if loose_pairs_result is None:
        return None

    loose_pairs, final_removes, final_adds = loose_pairs_result

    expanded: list[dict[str, Any]] = []
    full_path = join_json_pointer(path_prefix, path)

    for remove_op, add_op in [*exact_pairs, *loose_pairs]:
        expanded.extend(expand_replace_op(full_path, remove_op.get("value"), add_op.get("value")))

    for remove_op in final_removes:
        expanded.append({"op": "remove", "path": full_path, "value": remove_op.get("value")})
    for add_op in final_adds:
        expanded.append({"op": "add", "path": full_path, "value": add_op.get("value")})

    return expanded


def _jd_binary() -> str:
    jd = shutil.which("jd")
    if jd is None:
        raise RuntimeError("`jd` command not found. Install `jd` to use `dataclaw diff-jsonl`.")
    return jd


def run_jd_patch(old_obj: Any, new_obj: Any) -> list[dict[str, Any]]:
    jd = _jd_binary()
    with tempfile.TemporaryDirectory(prefix="jsonl-diff-") as temp_dir:
        temp_path = Path(temp_dir)
        old_path = temp_path / "old.json"
        new_path = temp_path / "new.json"
        old_path.write_bytes(canonical_record_bytes(old_obj))
        new_path.write_bytes(canonical_record_bytes(new_obj))
        result = subprocess.run(
            [jd, "-f", "patch", str(old_path), str(new_path)],
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.strip() or "jd failed")
    stdout = result.stdout.strip()
    if not stdout:
        return []
    patch_ops = orjson.loads(stdout)
    return simplify_patch_ops(patch_ops)


def build_text_replace_diff(old: str, new: str) -> str | None:
    if old == new or ("\n" not in old and "\n" not in new):
        return None
    diff_lines = list(
        difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile="old", tofile="new", lineterm="", n=3)
    )
    if not diff_lines:
        return None
    return "\n".join(diff_lines)


def build_record_patch(old: Any, new: Any) -> list[dict[str, Any]]:
    if contains_large_binary_value(old) or contains_large_binary_value(new):
        return summarize_large_binary_patch_ops(
            run_jd_patch(prepare_large_binary_diff_value(old), prepare_large_binary_diff_value(new))
        )
    return run_jd_patch(old, new)


def _build_record_patch_worker(payload: tuple[Any, Any]) -> list[dict[str, Any]]:
    old, new = payload
    return build_record_patch(old, new)


def _resolve_modified_event_patches(
    modified_events: list[tuple[dict[str, Any], Any, Any]],
    workers: int | None = None,
) -> None:
    if not modified_events:
        return

    payloads = [(old_obj, new_obj) for _event, old_obj, new_obj in modified_events]
    resolved_workers = _resolve_diff_workers(len(payloads), workers)

    if resolved_workers <= 1:
        patches = [_build_record_patch_worker(payload) for payload in payloads]
    else:
        with ThreadPoolExecutor(max_workers=resolved_workers) as executor:
            patches = list(executor.map(_build_record_patch_worker, payloads))

    for (event, _old_obj, _new_obj), patch in zip(modified_events, patches, strict=True):
        event["patch"] = patch


def expand_replace_op(path: str, old: Any, new: Any) -> list[dict[str, Any]]:
    if old == new:
        return []

    if contains_large_binary_value(old) or contains_large_binary_value(new):
        return summarize_large_binary_patch_ops(
            expand_replace_op(path, prepare_large_binary_diff_value(old), prepare_large_binary_diff_value(new))
        )

    if isinstance(old, (dict, list)) and isinstance(new, type(old)):
        nested_patch = run_jd_patch(old, new)
        nested_ops = []
        for op in nested_patch:
            nested_op = dict(op)
            nested_op["path"] = join_json_pointer(path, nested_op["path"])
            nested_ops.append(nested_op)
        if nested_ops:
            return nested_ops

    if isinstance(old, str) and isinstance(new, str):
        text_diff = build_text_replace_diff(old, new)
        if text_diff is not None:
            return [{"op": "replace_text", "path": path, "diff": text_diff}]

    return [{"op": "replace", "path": path, "old": old, "new": new}]


def simplify_patch_ops(patch_ops: list[dict[str, Any]], path_prefix: str = "") -> list[dict[str, Any]]:
    filtered = [op for op in patch_ops if op.get("op") != "test"]
    simplified = []
    i = 0
    while i < len(filtered):
        j = i + 1
        while (
            j < len(filtered)
            and filtered[j].get("path") == filtered[i].get("path")
            and filtered[j].get("op") in {"remove", "add"}
            and filtered[i].get("op") in {"remove", "add"}
        ):
            j += 1
        run_ops = filtered[i:j]
        expanded_run = expand_array_item_run(run_ops, path_prefix)
        if expanded_run is not None:
            simplified.extend(expanded_run)
            i = j
            continue

        op = filtered[i]
        next_op = filtered[i + 1] if i + 1 < len(filtered) else None
        if (
            op.get("op") == "remove"
            and next_op
            and next_op.get("op") == "add"
            and next_op.get("path") == op.get("path")
        ):
            simplified.extend(
                expand_replace_op(join_json_pointer(path_prefix, op["path"]), op.get("value"), next_op.get("value"))
            )
            i += 2
            continue

        item = {"op": op.get("op"), "path": join_json_pointer(path_prefix, op.get("path", ""))}
        if "value" in op:
            item["value"] = op["value"]
        simplified.append(item)
        i += 1
    return simplified


def build_events(
    old_index: FileIndex,
    new_index: FileIndex,
    old_records: dict[tuple[Any, ...], dict[str, Any]],
    new_records: dict[tuple[Any, ...], dict[str, Any]],
    include_records_for_modified: bool,
    emit_event: Callable[[dict[str, Any]], None],
    workers: int | None = None,
) -> tuple[int, dict[str, int]]:
    event_count = 0
    events: list[dict[str, Any]] = []
    modified_events: list[tuple[dict[str, Any], Any, Any]] = []
    summary = {
        "unchanged_records": 0,
        "modified_records": 0,
        "added_records": 0,
        "removed_records": 0,
    }

    for key in order_keys(old_index, new_index):
        old_counts = old_index.groups.get(key, {}).get("counts", Counter())
        new_counts = new_index.groups.get(key, {}).get("counts", Counter())

        if old_counts == new_counts:
            summary["unchanged_records"] += sum(new_counts.values())
            continue

        old_only = old_counts - new_counts
        new_only = new_counts - old_counts
        paired_old = Counter()
        paired_new = Counter()

        for old_hash, new_hash in zip(iter_expanded_hashes(old_only), iter_expanded_hashes(new_only)):
            paired_old[old_hash] += 1
            paired_new[new_hash] += 1
            old_entry = old_records[key][old_hash]
            new_entry = new_records[key][new_hash]
            event = {
                "change_type": "modified",
                "identity": identity_dict(key),
                "old_line": _pop_line_number(old_entry),
                "new_line": _pop_line_number(new_entry),
                "patch": [],
            }
            if include_records_for_modified:
                event["old_record"] = old_entry["obj"]
                event["new_record"] = new_entry["obj"]
            events.append(event)
            modified_events.append((event, old_entry["obj"], new_entry["obj"]))
            event_count += 1
            summary["modified_records"] += 1

        old_leftovers = old_only - paired_old
        new_leftovers = new_only - paired_new

        for digest in sorted(old_leftovers):
            count = old_leftovers[digest]
            if count <= 0:
                continue
            entry = old_records[key][digest]
            lines = _take_line_numbers(entry, count)
            events.append(
                {
                    "change_type": "removed",
                    "identity": identity_dict(key),
                    "old_lines": lines,
                    "occurrences": count,
                    "record": entry["obj"],
                }
            )
            event_count += 1
            summary["removed_records"] += count

        for digest in sorted(new_leftovers):
            count = new_leftovers[digest]
            if count <= 0:
                continue
            entry = new_records[key][digest]
            lines = _take_line_numbers(entry, count)
            events.append(
                {
                    "change_type": "added",
                    "identity": identity_dict(key),
                    "new_lines": lines,
                    "occurrences": count,
                    "record": entry["obj"],
                }
            )
            event_count += 1
            summary["added_records"] += count

    _resolve_modified_event_patches(modified_events, workers)

    for event in events:
        emit_event(event)

    return event_count, summary


def diff_jsonl_files(
    old_path: Path,
    new_path: Path,
    output_path: Path | None = None,
    *,
    include_records_for_modified: bool = False,
    workers: int | None = None,
) -> DiffResult:
    if output_path is None:
        output_path = default_diff_output_path(new_path)

    old_index = index_jsonl(old_path)
    new_index = index_jsonl(new_path)
    changed_keys = collect_changed_keys(old_index, new_index)
    changed_key_set = set(changed_keys)

    old_records = load_records_for_keys(old_path, changed_key_set)
    new_records = load_records_for_keys(new_path, changed_key_set)

    with tempfile.TemporaryDirectory(prefix="jsonl-diff-events-") as temp_dir:
        events_path = Path(temp_dir) / "events.yaml"
        with _open_text_output(events_path) as events_handle:
            events_decoded_handle = DecodeStream(events_handle)
            event_count, event_summary = build_events(
                old_index,
                new_index,
                old_records,
                new_records,
                include_records_for_modified=include_records_for_modified,
                emit_event=lambda event: _yaml_dump_document(event, events_handle, events_decoded_handle),
                workers=workers,
            )

        summary = {
            "old_records": old_index.total_records,
            "new_records": new_index.total_records,
            "changed_identity_keys": len(changed_keys),
            **event_summary,
        }
        header = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "old_file": str(old_path),
            "new_file": str(new_path),
            "identity_fields": list(IDENTITY_FIELDS),
            "summary": summary,
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with _open_text_output(output_path) as output_handle:
            output_decoded_handle = DecodeStream(output_handle)
            _yaml_dump_document(header, output_handle, output_decoded_handle)
            with events_path.open(encoding="utf-8") as events_handle:
                shutil.copyfileobj(events_handle, output_handle)

    return DiffResult(output_path=output_path, event_count=event_count, summary=summary)
