"""Tests for the workbench SQLite index."""

import json

import pytest

from dataclaw.index import (
    add_policy,
    create_bundle,
    get_bundle,
    get_bundles,
    get_policies,
    get_session_detail,
    get_stats,
    open_index,
    query_sessions,
    remove_policy,
    search_fts,
    update_session,
    upsert_sessions,
)


@pytest.fixture
def index_conn(tmp_path, monkeypatch):
    """Open an index DB in a temp directory."""
    monkeypatch.setattr("dataclaw.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("dataclaw.index.BLOBS_DIR", tmp_path / "blobs")
    conn = open_index()
    yield conn
    conn.close()


def _make_session(session_id="sess-1", project="test-project", source="claude",
                  model="claude-sonnet-4", content="Fix the login bug"):
    return {
        "session_id": session_id,
        "project": project,
        "source": source,
        "model": model,
        "start_time": "2025-01-01T00:00:00+00:00",
        "end_time": "2025-01-01T00:10:00+00:00",
        "git_branch": "main",
        "messages": [
            {"role": "user", "content": content, "tool_uses": []},
            {"role": "assistant", "content": "I'll fix it.", "tool_uses": [
                {"tool": "bash", "input": {"command": "pytest"}, "output": "1 passed", "status": "success"},
            ]},
        ],
        "stats": {
            "user_messages": 1,
            "assistant_messages": 1,
            "tool_uses": 1,
            "input_tokens": 500,
            "output_tokens": 100,
        },
    }


class TestUpsertSessions:
    def test_insert_new_session(self, index_conn):
        sessions = [_make_session()]
        new_count = upsert_sessions(index_conn, sessions)
        assert new_count == 1

    def test_insert_multiple_sessions(self, index_conn):
        sessions = [
            _make_session("s1", content="First task"),
            _make_session("s2", content="Second task"),
        ]
        new_count = upsert_sessions(index_conn, sessions)
        assert new_count == 2

    def test_upsert_preserves_review_status(self, index_conn):
        upsert_sessions(index_conn, [_make_session()])
        update_session(index_conn, "sess-1", status="approved")

        # Re-index same session
        upsert_sessions(index_conn, [_make_session()])

        row = index_conn.execute(
            "SELECT review_status FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["review_status"] == "approved"

    def test_skips_session_without_id(self, index_conn):
        session = _make_session()
        del session["session_id"]
        assert upsert_sessions(index_conn, [session]) == 0

    def test_empty_list(self, index_conn):
        assert upsert_sessions(index_conn, []) == 0

    def test_badges_computed(self, index_conn):
        upsert_sessions(index_conn, [_make_session()])
        row = index_conn.execute(
            "SELECT outcome_badge, task_type, display_title FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["display_title"] == "Fix the login bug"
        assert row["outcome_badge"] is not None
        assert row["task_type"] is not None


class TestQuerySessions:
    def test_query_all(self, index_conn):
        upsert_sessions(index_conn, [_make_session("s1"), _make_session("s2")])
        results = query_sessions(index_conn)
        assert len(results) == 2

    def test_filter_by_status(self, index_conn):
        upsert_sessions(index_conn, [_make_session("s1"), _make_session("s2")])
        update_session(index_conn, "s1", status="approved")

        results = query_sessions(index_conn, status="approved")
        assert len(results) == 1
        assert results[0]["session_id"] == "s1"

    def test_filter_by_source(self, index_conn):
        upsert_sessions(index_conn, [
            _make_session("s1", source="claude"),
            _make_session("s2", source="codex"),
        ])
        results = query_sessions(index_conn, source="codex")
        assert len(results) == 1
        assert results[0]["source"] == "codex"

    def test_limit_and_offset(self, index_conn):
        sessions = [_make_session(f"s{i}") for i in range(10)]
        upsert_sessions(index_conn, sessions)

        results = query_sessions(index_conn, limit=3, offset=0)
        assert len(results) == 3

        results2 = query_sessions(index_conn, limit=3, offset=3)
        assert len(results2) == 3
        assert results[0]["session_id"] != results2[0]["session_id"]


class TestGetSessionDetail:
    def test_returns_messages(self, index_conn):
        upsert_sessions(index_conn, [_make_session()])
        detail = get_session_detail(index_conn, "sess-1")
        assert detail is not None
        assert len(detail["messages"]) == 2
        assert detail["messages"][0]["role"] == "user"

    def test_not_found(self, index_conn):
        assert get_session_detail(index_conn, "nonexistent") is None


class TestUpdateSession:
    def test_update_status(self, index_conn):
        upsert_sessions(index_conn, [_make_session()])
        ok = update_session(index_conn, "sess-1", status="shortlisted")
        assert ok is True

        row = index_conn.execute(
            "SELECT review_status, reviewed_at FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["review_status"] == "shortlisted"
        assert row["reviewed_at"] is not None

    def test_update_notes(self, index_conn):
        upsert_sessions(index_conn, [_make_session()])
        update_session(index_conn, "sess-1", notes="Good trace", reason="strong debugging")

        row = index_conn.execute(
            "SELECT reviewer_notes, selection_reason FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["reviewer_notes"] == "Good trace"
        assert row["selection_reason"] == "strong debugging"

    def test_not_found(self, index_conn):
        assert update_session(index_conn, "nope", status="blocked") is False


class TestStats:
    def test_stats(self, index_conn):
        upsert_sessions(index_conn, [
            _make_session("s1", source="claude"),
            _make_session("s2", source="codex"),
        ])
        stats = get_stats(index_conn)
        assert stats["total"] == 2
        assert stats["by_source"]["claude"] == 1
        assert stats["by_source"]["codex"] == 1
        assert stats["by_status"]["new"] == 2


class TestBundles:
    def test_create_and_get(self, index_conn):
        upsert_sessions(index_conn, [_make_session("s1"), _make_session("s2")])
        bundle_id = create_bundle(index_conn, ["s1", "s2"], note="Test bundle")

        bundle = get_bundle(index_conn, bundle_id)
        assert bundle is not None
        assert bundle["session_count"] == 2
        assert bundle["submission_note"] == "Test bundle"
        assert len(bundle["sessions"]) == 2

    def test_list_bundles(self, index_conn):
        upsert_sessions(index_conn, [_make_session()])
        create_bundle(index_conn, ["sess-1"])
        bundles = get_bundles(index_conn)
        assert len(bundles) == 1

    def test_nonexistent_sessions(self, index_conn):
        bundle_id = create_bundle(index_conn, ["nonexistent"])
        bundle = get_bundle(index_conn, bundle_id)
        assert bundle["session_count"] == 0


class TestPolicies:
    def test_add_and_list(self, index_conn):
        pid = add_policy(index_conn, "redact_string", "my-secret", reason="API key")
        policies = get_policies(index_conn)
        assert len(policies) == 1
        assert policies[0]["policy_id"] == pid
        assert policies[0]["value"] == "my-secret"

    def test_remove(self, index_conn):
        pid = add_policy(index_conn, "exclude_project", "private-repo")
        assert remove_policy(index_conn, pid) is True
        assert len(get_policies(index_conn)) == 0

    def test_remove_nonexistent(self, index_conn):
        assert remove_policy(index_conn, "nope") is False
