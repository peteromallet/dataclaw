"""Structured logging helpers for DataClaw."""

import atexit
from datetime import datetime, timezone
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any

_atexit_registered = False

LOG_DIR = Path.home() / ".dataclaw" / "logs"
_HF_TOKEN_RE = re.compile(r"hf_[A-Za-z0-9_-]{20,}")
_HF_ENV_RE = re.compile(r"(HF_TOKEN\s*[=:]\s*)\S+", re.IGNORECASE)
_VALID_RESULTS = {"pushed", "noop", "dry-run", "error", "blocked"}


def _scrub_secrets(text: str) -> str:
    text = _HF_ENV_RE.sub(r"\1[REDACTED]", text)
    return _HF_TOKEN_RE.sub("[REDACTED]", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": _scrub_secrets(record.getMessage()),
        }
        for key in ("run_id", "phase", "session_id", "project", "source", "extra"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = _scrub_secrets(self.formatException(record.exc_info))
        return json.dumps(payload, separators=(",", ":"), default=str)


class MergingLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        extra = dict(self.extra)
        supplied = kwargs.get("extra")
        if isinstance(supplied, dict):
            extra.update(supplied)
        kwargs["extra"] = extra
        return msg, kwargs


class DailyNamedHandler(TimedRotatingFileHandler):
    def __init__(self, log_dir: Path | str = LOG_DIR, backupCount: int = 30, delay: bool = False):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        filename = self._log_dir / f"auto-{today}.jsonl"
        super().__init__(
            filename,
            when="midnight",
            interval=1,
            backupCount=backupCount,
            encoding="utf-8",
            delay=delay,
            utc=True,
        )

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None

        new_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        self.baseFilename = str(self._log_dir / f"auto-{new_date}.jsonl")

        if self.backupCount > 0:
            files = sorted(self._log_dir.glob("auto-*.jsonl"))
            for old in files[:max(0, len(files) - self.backupCount)]:
                try:
                    old.unlink()
                except OSError:
                    pass

        current_time = int(time.time())
        new_rollover_at = self.computeRollover(current_time)
        while new_rollover_at <= current_time:
            new_rollover_at += self.interval
        self.rolloverAt = new_rollover_at

        if not self.delay:
            self.stream = self._open()


class FlushingDailyNamedHandler(DailyNamedHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logging(
    run_id: str,
    level: str | int = "INFO",
    *,
    log_dir: Path | str | None = None,
) -> logging.LoggerAdapter:
    target_dir = Path(log_dir) if log_dir is not None else LOG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("dataclaw")
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    file_handler = FlushingDailyNamedHandler(target_dir)
    file_handler.setLevel(level)
    file_handler.setFormatter(JsonFormatter())

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(JsonFormatter())

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    global _atexit_registered
    if not _atexit_registered:
        atexit.register(logging.shutdown)
        _atexit_registered = True

    return MergingLoggerAdapter(logger, {"run_id": run_id})


def write_run_summary(run_dir: Path | str, summary: dict[str, Any]) -> Path:
    required = {
        "run_id",
        "started_at",
        "finished_at",
        "result",
        "total_sessions_new",
        "staging_dir",
    }
    missing = sorted(required - summary.keys())
    if missing:
        raise ValueError(f"RUN_SUMMARY missing required keys: {', '.join(missing)}")
    if summary["result"] not in _VALID_RESULTS:
        raise ValueError(f"Invalid RUN_SUMMARY result: {summary['result']}")

    payload = dict(summary)
    payload.setdefault("warnings", [])
    root = Path(run_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "RUN_SUMMARY.json"

    fd, tmp_name = tempfile.mkstemp(prefix=".RUN_SUMMARY.", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path
