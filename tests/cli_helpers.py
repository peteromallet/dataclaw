"""Shared helpers for CLI tests."""

from dataclaw import _json as json


def extract_json_payload(stdout: str) -> dict:
    start = stdout.find("{")
    assert start >= 0, f"No JSON payload found in output: {stdout!r}"
    return json.loads(stdout[start:])
