"""Tests for structured DataClaw logging."""

import json
import logging
import stat
import time

from freezegun import freeze_time
import pytest

from dataclaw.logging import DailyNamedHandler, setup_logging, write_run_summary


@freeze_time("2026-04-25T12:00:00Z")
def test_logging_writes_to_dated_filename_today(tmp_path):
    logger = setup_logging("run-1", log_dir=tmp_path)

    logger.info("hello")

    path = tmp_path / "auto-2026-04-25.jsonl"
    assert path.exists()
    row = json.loads(path.read_text().splitlines()[0])
    assert row["level"] == "INFO"
    assert row["run_id"] == "run-1"


def test_logging_rotates_to_new_dated_filename_at_midnight(tmp_path):
    with freeze_time("2026-04-25T23:59:59Z"):
        logger = setup_logging("run-rotate", log_dir=tmp_path)
        logger.info("before")
        handler = _daily_handler()

    with freeze_time("2026-04-26T00:00:01Z"):
        handler.doRollover()
        logger.info("after")

    before = tmp_path / "auto-2026-04-25.jsonl"
    after = tmp_path / "auto-2026-04-26.jsonl"
    assert before.exists()
    assert after.exists()
    assert "before" in before.read_text()
    assert "after" in after.read_text()


@freeze_time("2026-04-25T23:59:59Z")
def test_logging_rolloverAt_advances_after_rollover(tmp_path):
    setup_logging("run-rollover", log_dir=tmp_path)
    handler = _daily_handler()
    old_rollover = handler.rolloverAt

    with freeze_time("2026-04-26T00:00:01Z"):
        handler.doRollover()
        record = logging.LogRecord("dataclaw", logging.INFO, __file__, 1, "next", (), None)
        assert handler.rolloverAt != old_rollover
        assert handler.rolloverAt > int(time.time())
        assert handler.shouldRollover(record) == 0


@freeze_time("2026-05-30T00:00:01Z")
def test_logging_prunes_beyond_backup_count(tmp_path):
    for day in range(1, 33):
        (tmp_path / f"auto-2026-04-{day:02d}.jsonl").write_text("{}\n")
    handler = DailyNamedHandler(tmp_path, backupCount=30)

    handler.doRollover()

    assert len(list(tmp_path.glob("auto-*.jsonl"))) <= 31


@freeze_time("2026-04-25T12:00:00Z")
def test_token_scrubbed_from_logs(tmp_path):
    logger = setup_logging("run-secret", log_dir=tmp_path)

    logger.info("HF_TOKEN=hf_aaaaaaaaaaaaaaaaaaaaa")
    logger.info("got hf_bbbbbbbbbbbbbbbbbbbbb back")

    text = (tmp_path / "auto-2026-04-25.jsonl").read_text()
    assert "HF_TOKEN=[REDACTED]" in text
    assert "got [REDACTED] back" in text
    assert "hf_aaaaaaaaaaaaaaaaaaaaa" not in text
    assert "hf_bbbbbbbbbbbbbbbbbbbbb" not in text


def test_run_summary_written(tmp_path):
    path = write_run_summary(tmp_path, {
        "run_id": "run-1",
        "started_at": "2026-04-25T12:00:00Z",
        "finished_at": "2026-04-25T12:01:00Z",
        "result": "dry-run",
        "total_sessions_new": 3,
        "staging_dir": str(tmp_path),
    })

    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload["warnings"] == []
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_run_summary_missing_required_key_raises(tmp_path):
    with pytest.raises(ValueError):
        write_run_summary(tmp_path, {
            "run_id": "run-1",
            "started_at": "2026-04-25T12:00:00Z",
            "finished_at": "2026-04-25T12:01:00Z",
            "total_sessions_new": 3,
            "staging_dir": str(tmp_path),
        })


def _daily_handler():
    for handler in logging.getLogger("dataclaw").handlers:
        if isinstance(handler, DailyNamedHandler):
            return handler
    raise AssertionError("DailyNamedHandler not attached")
