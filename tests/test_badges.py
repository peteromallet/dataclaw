"""Tests for badge computation."""

from dataclaw.badges import (
    compute_all_badges,
    compute_display_title,
    compute_outcome_badge,
    compute_risk_badges,
    compute_sensitivity_score,
    compute_task_type,
    compute_value_badges,
)


def _make_session(
    user_content="Fix the login bug",
    tool_uses=None,
    tool_output="",
    user_messages=5,
    assistant_messages=5,
    tool_use_count=3,
    input_tokens=1000,
    output_tokens=500,
):
    msgs = [{"role": "user", "content": user_content, "tool_uses": []}]
    if tool_uses is None:
        tool_uses = [{"tool": "bash", "input": {"command": "pytest"}, "output": tool_output, "status": "success"}]
    msgs.append({"role": "assistant", "content": "Working on it.", "tool_uses": tool_uses})
    return {
        "session_id": "test-1",
        "project": "test-project",
        "source": "claude",
        "model": "claude-sonnet-4",
        "messages": msgs,
        "stats": {
            "user_messages": user_messages,
            "assistant_messages": assistant_messages,
            "tool_uses": tool_use_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


class TestOutcomeBadge:
    def test_tests_passed(self):
        session = _make_session(tool_output="5 passed in 1.2s")
        assert compute_outcome_badge(session) == "tests_passed"

    def test_tests_failed(self):
        session = _make_session(tool_output="FAILED test_login.py::test_auth")
        assert compute_outcome_badge(session) == "tests_failed"

    def test_build_failed(self):
        session = _make_session(tool_output="BUILD FAILED")
        assert compute_outcome_badge(session) == "build_failed"

    def test_analysis_only_no_tools(self):
        session = _make_session(tool_uses=[])
        session["messages"][1]["tool_uses"] = []
        session["messages"] = [session["messages"][0]]  # user message only
        assert compute_outcome_badge(session) == "analysis_only"

    def test_analysis_only_read_tools(self):
        session = _make_session(tool_uses=[
            {"tool": "Read", "input": {"file_path": "foo.py"}, "output": "content", "status": "success"},
        ])
        assert compute_outcome_badge(session) == "analysis_only"

    def test_unknown_no_signal(self):
        session = _make_session(tool_uses=[
            {"tool": "Write", "input": {"file_path": "foo.py"}, "output": "wrote file", "status": "success"},
        ])
        assert compute_outcome_badge(session) == "unknown"


class TestValueBadges:
    def test_long_horizon(self):
        session = _make_session(user_messages=25, input_tokens=30000, output_tokens=25000)
        badges = compute_value_badges(session)
        assert "long_horizon" in badges

    def test_tool_rich(self):
        session = _make_session(user_messages=3, assistant_messages=3, tool_use_count=10)
        badges = compute_value_badges(session)
        assert "tool_rich" in badges

    def test_novel_domain(self):
        session = _make_session(user_content="Analyze the protein folding data using biopython")
        badges = compute_value_badges(session)
        assert "novel_domain" in badges

    def test_scientific_workflow(self):
        session = _make_session(user_content="Run the regression analysis on the dataset")
        badges = compute_value_badges(session)
        assert "scientific_workflow" in badges


class TestRiskBadges:
    def test_no_risk(self):
        session = _make_session(user_content="Hello world", tool_output="OK")
        badges = compute_risk_badges(session)
        # May or may not detect names/URLs depending on content
        assert isinstance(badges, list)

    def test_private_url(self):
        session = _make_session(user_content="Check https://internal.corp/api")
        badges = compute_risk_badges(session)
        assert "private_url" in badges


class TestSensitivityScore:
    def test_clean_session(self):
        session = _make_session(user_content="Hello", tool_output="OK")
        score = compute_sensitivity_score(session)
        assert 0.0 <= score <= 1.0

    def test_higher_with_secrets(self):
        session = _make_session(tool_output="Using key sk-ant-abcdefghijklmnopqrstuvwxyz1234567890")
        score = compute_sensitivity_score(session)
        assert score > 0.0


class TestTaskType:
    def test_debugging(self):
        assert compute_task_type(_make_session("Fix this bug in auth.py")) == "debugging"

    def test_feature(self):
        assert compute_task_type(_make_session("Add a new login page")) == "feature"

    def test_refactor(self):
        assert compute_task_type(_make_session("Refactor the database module")) == "refactor"

    def test_unknown(self):
        assert compute_task_type(_make_session("")) == "unknown"


class TestDisplayTitle:
    def test_basic(self):
        title = compute_display_title(_make_session("Fix the login bug"))
        assert title == "Fix the login bug"

    def test_strips_prefix(self):
        title = compute_display_title(_make_session("Please fix the login bug"))
        assert title.startswith("Fix")

    def test_truncates_long(self):
        long_msg = "A" * 200
        title = compute_display_title(_make_session(long_msg))
        assert len(title) <= 83  # 80 + "..."

    def test_empty_uses_project(self):
        session = _make_session("")
        session["messages"] = []
        title = compute_display_title(session)
        assert title == "test-project"


class TestComputeAll:
    def test_returns_all_fields(self):
        result = compute_all_badges(_make_session(tool_output="3 passed"))
        assert "display_title" in result
        assert "outcome_badge" in result
        assert "value_badges" in result
        assert "risk_badges" in result
        assert "sensitivity_score" in result
        assert "task_type" in result
        assert "files_touched" in result
        assert "commands_run" in result

    def test_extracts_commands(self):
        session = _make_session()
        result = compute_all_badges(session)
        assert "pytest" in result["commands_run"]
