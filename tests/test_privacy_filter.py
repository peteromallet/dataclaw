"""Tests for optional privacy-filter scanning."""

import builtins

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

    assert seen == {"device": "mps", "min_score": 0.7, "model": None}
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


def test_redact_session_redacts_oversized_sessions_without_model(monkeypatch):
    monkeypatch.setattr(pf, "_MAX_MODEL_SESSION_CHARS", 10)
    monkeypatch.setattr(pf, "_MAX_MODEL_SESSION_STRINGS", 10)
    load_calls = []
    monkeypatch.setattr(pf, "_load", lambda **kwargs: load_calls.append(kwargs) or (lambda _text: []))
    session = {
        "session_id": "s1",
        "source": "codex",
        "messages": [{"role": "user", "content": "very sensitive long text"}],
    }

    redacted, findings = pf.redact_session(session)

    # The model is never loaded for an oversized session; it is blanket-redacted.
    assert redacted["messages"][0]["content"] == "[REDACTED: oversized session]"
    assert findings[0].entity == "OVERSIZED_SESSION_REDACTED"
    assert load_calls == []
    # Original session dict is untouched (redact_session deep-copies).
    assert session["messages"][0]["content"] == "very sensitive long text"


def test_redact_session_redacts_content_parts(monkeypatch):
    monkeypatch.setattr(pf, "_load", lambda **_kw: _match_pipe("NAME", "Jane Doe"))
    session = {
        "messages": [{
            "content": "no match",
            "content_parts": [
                {"type": "text", "text": "Contact Jane Doe today"},
                {"type": "image", "url": "https://example.com/x.png"},
            ],
        }],
    }

    redacted, findings = pf.redact_session(session)

    # PII inside content_parts (which secrets.transform_session also walks) is redacted.
    assert redacted["messages"][0]["content_parts"][0]["text"] == "Contact [REDACTED] today"
    assert any(f.field and ".content_parts" in f.field for f in findings)


def test_scan_session_walks_content_parts(monkeypatch):
    monkeypatch.setattr(pf, "_load", lambda **_kw: _match_pipe("NAME", "Jane Doe"))
    session = {
        "messages": [{
            "content_parts": [{"type": "text", "text": "Ask Jane Doe"}],
        }],
    }

    fields = {finding.field for finding in pf.scan_session(session)}

    assert "messages[0].content_parts[0].text" in fields


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
def test_pipeline_runs_end_to_end_against_real_model():
    # Live smoke test: proves the default model id loads and the
    # token-classification pipeline (aggregation_strategy="simple") runs
    # end-to-end and returns offset-bearing findings. Detection *quality* of a
    # specific checkpoint under a specific transformers version is an open
    # verification item (see the Part B design doc), so we only assert the
    # integration contract here, not a minimum hit count.
    pytest.importorskip("transformers")
    pytest.importorskip("torch")

    findings = pf.scan_text("Hi, my name is John Smith and my email is john@example.com")

    assert isinstance(findings, list)
    for finding in findings:
        assert finding.start is None or isinstance(finding.start, int)
        assert finding.end is None or isinstance(finding.end, int)


def test_config_model_env_override(monkeypatch):
    monkeypatch.setenv("DATACLAW_PRIVACY_FILTER_MODEL", "acme/custom-pii")
    assert pf._config_model() == "acme/custom-pii"


def test_config_model_defaults_to_model_id(monkeypatch):
    monkeypatch.delenv("DATACLAW_PRIVACY_FILTER_MODEL", raising=False)
    monkeypatch.setattr(pf, "_config_model", pf._config_model)  # no-op, keep real fn
    # With no config file / no override, falls back to the default model id.
    monkeypatch.setattr("dataclaw.config.load_config", lambda: {})
    assert pf._config_model() == pf.MODEL_ID == pf.DEFAULT_MODEL_ID


# --- export-time wiring (graceful degradation) ---------------------------------

from dataclaw._cli import exporting as _exp  # noqa: E402


def test_apply_model_privacy_filter_disabled_is_noop():
    session = {"messages": [{"content": "Jane Doe"}]}
    cfg = _exp._PrivacyFilterConfig(enabled=False)

    assert _exp._apply_model_privacy_filter(session, cfg) is session


def test_apply_model_privacy_filter_graceful_when_unavailable(monkeypatch, capsys):
    # Force a fresh warning each run.
    monkeypatch.setattr(_exp, "_PF_WARNED", False)
    # Simulate torch/transformers absent.
    monkeypatch.setattr("dataclaw.privacy_filter.is_available", lambda: False)

    session = {"messages": [{"role": "user", "content": "Mechanical [REDACTED] intact"}]}
    cfg = _exp._PrivacyFilterConfig(enabled=True)

    result = _exp._apply_model_privacy_filter(session, cfg)

    # Export does not crash; mechanical redaction is preserved unchanged.
    assert result == session
    assert result["messages"][0]["content"] == "Mechanical [REDACTED] intact"
    err = capsys.readouterr().err
    assert "model privacy filter skipped" in err


def test_apply_model_privacy_filter_runs_when_available(monkeypatch):
    monkeypatch.setattr("dataclaw.privacy_filter.is_available", lambda: True)
    monkeypatch.setattr(pf, "_load", lambda **_kw: _match_pipe("NAME", "Jane Doe"))

    session = {"messages": [{"role": "user", "content": "Call Jane Doe now"}]}
    cfg = _exp._PrivacyFilterConfig(enabled=True, min_score=0.5)

    result = _exp._apply_model_privacy_filter(session, cfg)

    assert result["messages"][0]["content"] == "Call [REDACTED] now"


def test_read_privacy_filter_config(monkeypatch):
    monkeypatch.setattr(
        "dataclaw.config.load_config",
        lambda: {"privacy_filter": {"enabled": True, "device": "mps", "min_score": 0.7, "model": "acme/m"}},
    )
    cfg = _exp._read_privacy_filter_config()

    assert cfg.enabled is True
    assert cfg.device == "mps"
    assert cfg.min_score == 0.7
    assert cfg.model == "acme/m"


def test_read_privacy_filter_config_defaults(monkeypatch):
    monkeypatch.setattr("dataclaw.config.load_config", lambda: {})
    cfg = _exp._read_privacy_filter_config()

    assert cfg.enabled is False
    assert cfg.device is None
    assert cfg.min_score == 0.85
    assert cfg.model is None


def test_carry_forward_redactor_applies_model_filter(monkeypatch):
    # Carried-forward remote records must get the model PII pass too, not just
    # mechanical redaction -- otherwise the bulk of a steady-state push ships
    # without model scrubbing.
    monkeypatch.setattr(_exp, "_read_privacy_filter_config", lambda: _exp._PrivacyFilterConfig(enabled=True))

    seen = []

    def fake_model_filter(session, pf_config):
        seen.append(pf_config.enabled)
        session["_model_filtered"] = True
        return session

    monkeypatch.setattr(_exp, "_apply_model_privacy_filter", fake_model_filter)

    redact_fn = _exp._build_carry_forward_redactor({"redact_strings": [], "redact_usernames": []})
    out = redact_fn({"source": "claude", "session_id": "r1", "messages": [{"role": "user", "content": "hi"}]})

    assert out.get("_model_filtered") is True
    assert seen == [True]


def test_redaction_policy_version_changes_with_policy():
    base = _exp._PrivacyFilterConfig(enabled=False)
    v0 = _exp.redaction_policy_version([], [], base)
    # Same inputs -> same version (deterministic).
    assert v0 == _exp.redaction_policy_version([], [], base)
    # Adding a redact string changes the version (policy tightened).
    assert _exp.redaction_policy_version(["AcmeCorp"], [], base) != v0
    # Enabling the model filter changes the version.
    assert _exp.redaction_policy_version([], [], _exp._PrivacyFilterConfig(enabled=True)) != v0


def test_carry_forward_skips_when_stamp_current(monkeypatch):
    # A record already stamped with the CURRENT policy version must be returned
    # verbatim without re-running redaction (the keystone scale optimization).
    monkeypatch.setattr(_exp, "_read_privacy_filter_config", lambda: _exp._PrivacyFilterConfig(enabled=False))

    redaction = {"redact_strings": [], "redact_usernames": []}
    version = _exp.redaction_policy_version([], [], _exp._PrivacyFilterConfig(enabled=False))

    transform_calls = []
    monkeypatch.setattr(
        _exp, "transform_session",
        lambda *a, **k: transform_calls.append(1) or (a[0], 0),
    )

    redact_fn = _exp._build_carry_forward_redactor(redaction)

    current = {"source": "claude", "session_id": "r1", "redaction_policy": version, "messages": []}
    out = redact_fn(current)
    assert out is current  # untouched
    assert transform_calls == []  # transform_session NOT called

    stale = {"source": "claude", "session_id": "r2", "redaction_policy": "OLD", "messages": []}
    redact_fn(stale)
    assert transform_calls == [1]  # stale record IS re-redacted
    assert stale["redaction_policy"] == version  # and re-stamped


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
