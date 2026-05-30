"""Tests for the union merge-on-upload helper (Part A)."""

import orjson

from dataclaw import jsonl_tools
from dataclaw.jsonl_tools import MergeStats, merge_identity_key, merge_jsonl_union


def _write_jsonl(path, records):
    with path.open("wb") as handle:
        for record in records:
            handle.write(orjson.dumps(record))
            handle.write(b"\n")


def _read_jsonl(path):
    return [orjson.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def _identity(record):
    """A no-op redact_fn used when re-redaction content does not matter."""
    return record


class TestMergeIdentityKey:
    def test_keys_on_source_and_session_id_when_present(self):
        record = {"source": "claude", "session_id": "s1", "project": "p", "start_time": "t"}
        assert merge_identity_key(record) == ("sid", "claude", "s1")

    def test_falls_back_to_full_identity_when_no_session_id(self):
        record = {"source": "claude", "project": "p", "start_time": "t"}
        key = merge_identity_key(record)
        assert key[0] == "identity"
        # full identity_key tuple follows
        assert key[1:] == jsonl_tools.identity_key(record)


class TestMergeJsonlUnion:
    def test_remote_only_carried_forward(self, tmp_path):
        remote = tmp_path / "remote.jsonl"
        local = tmp_path / "local.jsonl"
        out = tmp_path / "merged.jsonl"
        _write_jsonl(remote, [{"source": "claude", "session_id": "r1", "messages": [{"role": "user"}]}])
        _write_jsonl(local, [])

        stats = merge_jsonl_union(remote, local, out, redact_fn=_identity)

        merged = _read_jsonl(out)
        assert [r["session_id"] for r in merged] == ["r1"]
        assert stats.carried_forward == 1
        assert stats.added == 0
        assert stats.merged_total == 1 >= stats.remote_total

    def test_local_only_added(self, tmp_path):
        remote = tmp_path / "remote.jsonl"
        local = tmp_path / "local.jsonl"
        out = tmp_path / "merged.jsonl"
        _write_jsonl(remote, [])
        _write_jsonl(local, [{"source": "claude", "session_id": "l1", "messages": [{"role": "user"}]}])

        stats = merge_jsonl_union(remote, local, out, redact_fn=_identity)

        merged = _read_jsonl(out)
        assert [r["session_id"] for r in merged] == ["l1"]
        assert stats.added == 1
        assert stats.carried_forward == 0

    def test_both_superset_picks_more_messages(self, tmp_path):
        remote = tmp_path / "remote.jsonl"
        local = tmp_path / "local.jsonl"
        out = tmp_path / "merged.jsonl"
        _write_jsonl(
            remote,
            [{"source": "claude", "session_id": "s1", "messages": [{"role": "user"}]}],
        )
        _write_jsonl(
            local,
            [{"source": "claude", "session_id": "s1", "messages": [{"role": "user"}, {"role": "assistant"}]}],
        )

        stats = merge_jsonl_union(remote, local, out, redact_fn=_identity)

        merged = _read_jsonl(out)
        assert len(merged) == 1
        assert len(merged[0]["messages"]) == 2  # local superset won
        assert stats.updated == 1
        assert stats.merged_total == 1 >= stats.remote_total

    def test_both_remote_has_more_keeps_remote(self, tmp_path):
        remote = tmp_path / "remote.jsonl"
        local = tmp_path / "local.jsonl"
        out = tmp_path / "merged.jsonl"
        _write_jsonl(
            remote,
            [{"source": "claude", "session_id": "s1", "messages": [{"role": "user"}, {"role": "assistant"}]}],
        )
        _write_jsonl(
            local,
            [{"source": "claude", "session_id": "s1", "messages": [{"role": "user"}]}],
        )

        stats = merge_jsonl_union(remote, local, out, redact_fn=_identity)

        merged = _read_jsonl(out)
        assert len(merged) == 1
        assert len(merged[0]["messages"]) == 2  # remote superset kept
        assert stats.unchanged == 1
        assert stats.updated == 0

    def test_dedup_by_source_session_id_ignores_start_time_and_project_drift(self, tmp_path):
        # H1/H2: same (source, session_id) but drifted start_time + project.
        # Must NOT duplicate.
        remote = tmp_path / "remote.jsonl"
        local = tmp_path / "local.jsonl"
        out = tmp_path / "merged.jsonl"
        _write_jsonl(
            remote,
            [
                {
                    "source": "claude",
                    "session_id": "s1",
                    "project": "old-anon-name",
                    "start_time": "2025-01-01T00:00:00Z",
                    "messages": [{"role": "user"}],
                }
            ],
        )
        _write_jsonl(
            local,
            [
                {
                    "source": "claude",
                    "session_id": "s1",
                    "project": "new-anon-name",
                    "start_time": "2025-01-01T00:00:00+00:00",
                    "messages": [{"role": "user"}],
                }
            ],
        )

        stats = merge_jsonl_union(remote, local, out, redact_fn=_identity)

        merged = _read_jsonl(out)
        assert len(merged) == 1  # collapsed to one despite drift
        assert stats.merged_total == 1
        assert stats.merged_total >= stats.remote_total

    def test_original_file_preserved_on_carried_forward(self, tmp_path):
        remote = tmp_path / "remote.jsonl"
        local = tmp_path / "local.jsonl"
        out = tmp_path / "merged.jsonl"
        _write_jsonl(
            remote,
            [
                {
                    "source": "claude",
                    "session_id": "r1",
                    "messages": [{"role": "user", "originalFile": "secret-original-content"}],
                }
            ],
        )
        _write_jsonl(local, [])

        merge_jsonl_union(remote, local, out, redact_fn=_identity)

        merged = _read_jsonl(out)
        assert merged[0]["messages"][0]["originalFile"] == "secret-original-content"

    def test_reredaction_applied_to_carried_forward(self, tmp_path):
        remote = tmp_path / "remote.jsonl"
        local = tmp_path / "local.jsonl"
        out = tmp_path / "merged.jsonl"
        _write_jsonl(remote, [{"source": "claude", "session_id": "r1", "messages": [{"role": "user"}]}])
        _write_jsonl(local, [{"source": "claude", "session_id": "l1", "messages": [{"role": "user"}]}])

        called_on = []

        def fake_redact(record):
            called_on.append(record.get("session_id"))
            record["reredacted"] = True
            return record

        merge_jsonl_union(remote, local, out, redact_fn=fake_redact)

        # redact_fn must be called on the carried-forward remote record (r1) but not l1.
        assert called_on == ["r1"]
        merged = {r["session_id"]: r for r in _read_jsonl(out)}
        assert merged["r1"].get("reredacted") is True
        assert "reredacted" not in merged["l1"]

    def test_union_invariant_merged_ge_remote(self, tmp_path):
        remote = tmp_path / "remote.jsonl"
        local = tmp_path / "local.jsonl"
        out = tmp_path / "merged.jsonl"
        _write_jsonl(
            remote,
            [
                {"source": "claude", "session_id": "r1", "messages": [{"role": "user"}]},
                {"source": "claude", "session_id": "r2", "messages": [{"role": "user"}]},
            ],
        )
        # Local re-exports only r1 (e.g. narrower --source). r2 must survive.
        _write_jsonl(local, [{"source": "claude", "session_id": "r1", "messages": [{"role": "user"}]}])

        stats = merge_jsonl_union(remote, local, out, redact_fn=_identity)

        merged = {r["session_id"] for r in _read_jsonl(out)}
        assert merged == {"r1", "r2"}
        assert stats.merged_total >= stats.remote_total

    def test_duplicate_remote_keys_do_not_trip_invariant(self, tmp_path):
        # The old non-deduping uploader could leave duplicate (source, session_id)
        # lines in the remote. remote_total must count UNIQUE keys, not raw lines,
        # so dedup never makes merged_total < remote_total (which would permanently
        # block publishing).
        remote = tmp_path / "remote.jsonl"
        local = tmp_path / "local.jsonl"
        out = tmp_path / "merged.jsonl"
        _write_jsonl(
            remote,
            [
                {"source": "claude", "session_id": "r1", "messages": [{"role": "user"}]},
                {"source": "claude", "session_id": "r1", "messages": [{"role": "user"}]},  # dup line
            ],
        )
        _write_jsonl(local, [])

        stats = merge_jsonl_union(remote, local, out, redact_fn=_identity)

        assert stats.remote_total == 1  # unique keys, not 2 raw lines
        assert stats.merged_total == 1
        assert stats.merged_total >= stats.remote_total  # invariant holds, no false abort

    def test_malformed_remote_line_preserved_not_dropped(self, tmp_path):
        # A corrupt remote line must be carried through verbatim (never dropped =
        # data loss, never raised = wedged pushes).
        remote = tmp_path / "remote.jsonl"
        local = tmp_path / "local.jsonl"
        out = tmp_path / "merged.jsonl"
        remote.write_bytes(
            orjson.dumps({"source": "claude", "session_id": "r1", "messages": [{"role": "user"}]})
            + b"\n"
            + b"{this is not valid json,,,}\n"
        )
        _write_jsonl(local, [])

        stats = merge_jsonl_union(remote, local, out, redact_fn=_identity)

        raw = out.read_bytes()
        assert b"{this is not valid json,,,}" in raw  # preserved verbatim
        assert stats.malformed_preserved == 1
        assert stats.merged_total >= stats.remote_total  # invariant still holds

    def test_changelog_line_format(self):
        stats = MergeStats(
            remote_total=3, local_total=2, merged_total=4, added=1, updated=1, carried_forward=2, unchanged=0
        )
        line = stats.changelog_line()
        assert "added 1" in line
        assert "carried_forward 2" in line
        assert "remote 3 -> merged 4" in line
