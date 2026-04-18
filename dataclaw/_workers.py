"""Shared worker configuration helpers."""

from __future__ import annotations

import os

DATACLAW_WORKERS_ENV = "DATACLAW_WORKERS"


def configured_workers() -> int | None:
    raw = os.environ.get(DATACLAW_WORKERS_ENV)
    if not raw:
        return None

    try:
        return int(raw)
    except ValueError:
        return None
