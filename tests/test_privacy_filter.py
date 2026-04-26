"""Tests for optional privacy-filter scanning."""

import builtins
import json

import pytest

from dataclaw import privacy_filter as pf


def test_fingerprint_deterministic():
    first = pf.Finding("NAME", "Jane Doe", 0.91, start=1, end=9)
    second = pf.Finding("NAME", "Jane Doe", 0.12, start=40, end=48)

    assert first.fingerprint() == second.fingerprint()


def test_diff_findings_splits_correctly():
    findings = [
        pf.Finding("NAME", "A", 0.9),
        pf.Finding("NAME", "B", 0.9),
        pf.Finding("ORG", "C", 0.9),
        pf.Finding("ORG", "D", 0.9),
    ]
    known = {
        findings[1].fingerprint(): {"count": 1},
        findings[3].fingerprint(): {"count": 1},
    }

    new, old = pf.diff_findings(findings, known)

    assert [f.text for f in new] == ["A", "C"]
    assert [f.text for f in old] == ["B", "D"]


def test_record_findings_increments():
    finding = pf.Finding("NAME", "Jane Doe", 0.9)

    registry = pf.record_findings([finding])
    first_seen = registry[finding.fingerprint()]["first_seen"]
    registry = pf.record_findings([finding], registry)

    record = registry[finding.fingerprint()]
    assert record["count"] == 2
    assert record["first_seen"] == first_seen
    assert record["last_seen"] >= first_seen


def test_is_available_false_without_deps(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "transformers":
            raise ImportError("No module named transformers")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert pf.is_available() is False


def test_scan_text_passes_device_and_min_score_through(monkeypatch):
    seen = {}

    def fake_load(**kwargs):
        seen.update(kwargs)
        return lambda _text: [
            {"entity_group": "NAME", "word": "Jane", "score": 0.8, "start": 0, "end": 4},
        ]

    monkeypatch.setattr(pf, "_load", fake_load)

    findings = pf.scan_text("Jane", device="mps", min_score=0.7)

    assert seen == {"device": "mps", "min_score": 0.7}
    assert findings[0].text == "Jane"


def test_scan_session_aggregates_messages_and_thinking(monkeypatch):
    monkeypatch.setattr(pf, "_load", lambda **_kw: _match_pipe("ORG", "ACME Corp"))
    session = {
        "messages": [
            {"content": "hello"},
            {"thinking": "Need to call ACME Corp before shipping."},
        ],
    }

    findings = pf.scan_session(session)

    assert len(findings) == 1
    assert findings[0].field.startswith("messages[")
    assert findings[0].field.endswith(".thinking")


def test_scan_session_walks_nested_tool_output_shapes(monkeypatch):
    monkeypatch.setattr(pf, "_load", lambda **_kw: _match_pipe("NAME", "Jane Doe"))
    session = {
        "messages": [{
            "tool_uses": [
                {"output": {"files": [{"content": "Jane Doe"}], "stdout": "Jane Doe"}},
                {"output": "Jane Doe"},
                {"input": {"command": "echo Jane Doe"}},
            ],
        }],
    }

    fields = {finding.field for finding in pf.scan_session(session)}

    assert "messages[0].tool_uses[0].output.files[0].content" in fields
    assert "messages[0].tool_uses[1].output" in fields
    assert "messages[0].tool_uses[2].input.command" in fields


def test_scan_session_can_skip_tool_io(monkeypatch):
    monkeypatch.setattr(pf, "_load", lambda **_kw: _match_pipe("NAME", "Jane Doe"))
    session = {
        "messages": [{
            "content": "No match here",
            "tool_uses": [{"output": "Jane Doe"}],
        }],
    }

    assert pf.scan_session(session, include_tool_io=False) == []


def test_redact_session_replaces_model_findings(monkeypatch):
    monkeypatch.setattr(pf, "_load", lambda **_kw: _match_pipe("NAME", "Jane Doe"))
    session = {"messages": [{"content": "Ask Jane Doe about this."}]}

    redacted, findings = pf.redact_session(session)

    assert len(findings) == 1
    assert redacted["messages"][0]["content"] == "Ask [REDACTED] about this."
    assert session["messages"][0]["content"] == "Ask Jane Doe about this."


def test_redact_session_can_limit_roles(monkeypatch):
    monkeypatch.setattr(pf, "_load", lambda **_kw: _match_pipe("NAME", "Jane Doe"))
    session = {
        "messages": [
            {"role": "assistant", "content": "Jane Doe from assistant"},
            {"role": "user", "content": "Jane Doe from user"},
        ],
    }

    redacted, findings = pf.redact_session(session, roles={"user"})

    assert len(findings) == 1
    assert redacted["messages"][0]["content"] == "Jane Doe from assistant"
    assert redacted["messages"][1]["content"] == "[REDACTED] from user"


def test_redact_jsonl_redacts_oversized_sessions_without_model(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "_MAX_MODEL_SESSION_CHARS", 10)
    monkeypatch.setattr(pf, "_MAX_MODEL_SESSION_STRINGS", 10)
    load_calls = []
    monkeypatch.setattr(pf, "_load", lambda **kwargs: load_calls.append(kwargs) or (lambda _text: []))
    path = tmp_path / "shard.jsonl"
    path.write_text(json.dumps({
        "session_id": "s1",
        "source": "codex",
        "messages": [{"role": "user", "content": "very sensitive long text"}],
    }) + "\n")
    events = []

    findings = pf.redact_jsonl(path, progress_callback=lambda event, payload: events.append((event, payload)), roles={"user"})

    row = json.loads(path.read_text())
    assert row["messages"][0]["content"] == "[REDACTED: oversized session]"
    assert findings[0].entity == "OVERSIZED_SESSION_REDACTED"
    assert load_calls == []
    assert any(event == "privacy_filter_session_size_guard_redacted" for event, _payload in events)


def test_dtype_auto_selects_bfloat16_on_mps(monkeypatch):
    torch = pytest.importorskip("torch")

    # Force the auto path even if a user has overridden dtype in their config.
    monkeypatch.setattr(pf, "_config_dtype", lambda: "auto")
    # Pretend we're on Apple Silicon with MPS available.
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
    # Reset the pipeline cache so the next _load triggers a fresh build.
    monkeypatch.setattr(pf, "_PIPELINES", {})

    captured: dict[str, object] = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return lambda _text: []

    monkeypatch.setattr(pf, "_build_pipeline", fake_build)

    pf._load(device="mps")

    assert captured.get("dtype") is torch.bfloat16
    assert captured.get("model") == pf.MODEL_ID
    assert captured.get("device") == "mps"


def test_load_defaults_to_mps_when_available(monkeypatch):
    torch = pytest.importorskip("torch")

    monkeypatch.setattr(pf, "_config_device", lambda: "auto")
    monkeypatch.setattr(pf, "_config_dtype", lambda: "auto")
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
    monkeypatch.setattr(pf, "_PIPELINES", {})

    captured: dict[str, object] = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return lambda _text: []

    monkeypatch.setattr(pf, "_build_pipeline", fake_build)

    pf._load()

    assert captured.get("device") == "mps"
    assert captured.get("dtype") is torch.bfloat16


def test_dtype_auto_falls_back_to_fp32_on_cpu(monkeypatch):
    torch = pytest.importorskip("torch")

    monkeypatch.setattr(pf, "_config_dtype", lambda: "auto")
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
    monkeypatch.setattr(pf, "_PIPELINES", {})

    captured: dict[str, object] = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return lambda _text: []

    monkeypatch.setattr(pf, "_build_pipeline", fake_build)

    pf._load()

    assert captured.get("dtype") is torch.float32


@pytest.mark.pii
def test_pipeline_decoder_matches_model_card_examples():
    pytest.importorskip("transformers")

    findings = pf.scan_text("Hi, my name is John Smith")

    assert findings


def _match_pipe(entity: str, needle: str):
    def pipe(text):
        start = text.find(needle)
        if start < 0:
            return []
        return [{
            "entity_group": entity,
            "word": needle,
            "score": 0.99,
            "start": start,
            "end": start + len(needle),
        }]

    return pipe
