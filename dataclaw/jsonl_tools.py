"""Utilities for rendering and diffing DataClaw JSONL exports."""

from __future__ import annotations

import difflib
import hashlib
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson
import yaml

IDENTITY_FIELDS = ("source", "project", "session_id", "start_time")
OMITTED_ORIGINAL_FILE = "<omitted originalFile content>"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
DEFAULT_YAML_SUFFIX = "_formatted.yaml"
DEFAULT_DIFF_SUFFIX = "_diff.yaml"
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


def yaml_dump_documents(documents: list[dict[str, Any]], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        decoded_handle = DecodeStream(handle)
        for document in documents:
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
    return output_path


def jsonl_to_yaml_file(input_path: Path, output_path: Path | None = None) -> Path:
    if output_path is None:
        output_path = default_yaml_output_path(input_path)

    documents = []
    with input_path.open("rb") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            documents.append(orjson.loads(line))

    return yaml_dump_documents(documents, output_path)


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
                group = {"first_line": line_number, "counts": Counter(), "hash_first_line": {}}
                groups[key] = group
            group["counts"][digest] += 1
            group["hash_first_line"].setdefault(digest, line_number)
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
                entry = {"obj": obj, "line_numbers": []}
                group[digest] = entry
            entry["line_numbers"].append(line_number)
    return records


def expand_hashes(counter: Counter[str]) -> list[str]:
    expanded = []
    for digest in sorted(counter):
        expanded.extend([digest] * counter[digest])
    return expanded


def join_json_pointer(path_prefix: str, child_path: str) -> str:
    if not path_prefix:
        return child_path
    if not child_path:
        return path_prefix
    if child_path.startswith("/"):
        return f"{path_prefix}{child_path}"
    return f"{path_prefix}/{child_path}"


def match_key_for_array_item(value: Any) -> tuple[Any, ...] | None:
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

    remove_buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    add_buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for op in removes:
        key = match_key_for_array_item(op.get("value"))
        if key is None:
            return None
        remove_buckets.setdefault(key, []).append(op)
    for op in adds:
        key = match_key_for_array_item(op.get("value"))
        if key is None:
            return None
        add_buckets.setdefault(key, []).append(op)

    if set(remove_buckets) != set(add_buckets):
        return None

    expanded: list[dict[str, Any]] = []
    full_path = join_json_pointer(path_prefix, path)
    for key in sorted(remove_buckets, key=str):
        remove_items = remove_buckets[key]
        add_items = add_buckets[key]
        if len(remove_items) != len(add_items):
            return None
        for remove_op, add_op in zip(remove_items, add_items, strict=True):
            expanded.extend(expand_replace_op(full_path, remove_op.get("value"), add_op.get("value")))
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
        difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile="old", tofile="new", lineterm="", n=2)
    )
    if not diff_lines:
        return None
    return "\n".join(diff_lines)


def expand_replace_op(path: str, old: Any, new: Any) -> list[dict[str, Any]]:
    if old == new:
        return []

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
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    events: list[dict[str, Any]] = []
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
        old_unmatched = expand_hashes(old_only)
        new_unmatched = expand_hashes(new_only)

        while old_unmatched and new_unmatched:
            old_hash = old_unmatched.pop(0)
            new_hash = new_unmatched.pop(0)
            old_entry = old_records[key][old_hash]
            new_entry = new_records[key][new_hash]
            event = {
                "change_type": "modified",
                "identity": identity_dict(key),
                "old_line": old_entry["line_numbers"].pop(0),
                "new_line": new_entry["line_numbers"].pop(0),
                "patch": run_jd_patch(old_entry["obj"], new_entry["obj"]),
            }
            if include_records_for_modified:
                event["old_record"] = old_entry["obj"]
                event["new_record"] = new_entry["obj"]
            events.append(event)
            summary["modified_records"] += 1

        old_leftovers = Counter(old_unmatched)
        new_leftovers = Counter(new_unmatched)

        for digest, count in old_leftovers.items():
            if count <= 0:
                continue
            entry = old_records[key][digest]
            lines = entry["line_numbers"][:count]
            del entry["line_numbers"][:count]
            events.append(
                {
                    "change_type": "removed",
                    "identity": identity_dict(key),
                    "old_lines": lines,
                    "occurrences": count,
                    "record": entry["obj"],
                }
            )
            summary["removed_records"] += count

        for digest, count in new_leftovers.items():
            if count <= 0:
                continue
            entry = new_records[key][digest]
            lines = entry["line_numbers"][:count]
            del entry["line_numbers"][:count]
            events.append(
                {
                    "change_type": "added",
                    "identity": identity_dict(key),
                    "new_lines": lines,
                    "occurrences": count,
                    "record": entry["obj"],
                }
            )
            summary["added_records"] += count

    return events, summary


def diff_jsonl_files(
    old_path: Path,
    new_path: Path,
    output_path: Path | None = None,
    *,
    include_records_for_modified: bool = False,
) -> DiffResult:
    if output_path is None:
        output_path = default_diff_output_path(new_path)

    old_index = index_jsonl(old_path)
    new_index = index_jsonl(new_path)
    changed_keys = collect_changed_keys(old_index, new_index)
    changed_key_set = set(changed_keys)

    old_records = load_records_for_keys(old_path, changed_key_set)
    new_records = load_records_for_keys(new_path, changed_key_set)

    events, event_summary = build_events(
        old_index,
        new_index,
        old_records,
        new_records,
        include_records_for_modified=include_records_for_modified,
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
    yaml_dump_documents([header, *events], output_path)
    return DiffResult(output_path=output_path, event_count=len(events), summary=summary)
