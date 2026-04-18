"""Tests for CLI review helpers."""

import json

from dataclaw._cli import review as review_mod
from dataclaw._cli.review import (
    _collect_review_attestations,
    _scan_export_review,
    _scan_for_text_occurrences,
    _scan_high_entropy_strings,
    _scan_pii,
    _validate_publish_attestation,
    confirm,
)


class TestAttestationHelpers:
    def test_resolve_review_workers_uses_shared_env(self, monkeypatch):
        monkeypatch.setenv("DATACLAW_WORKERS", "3")

        assert review_mod._resolve_review_workers(32 * 1024 * 1024) == 3

    def test_collect_review_attestations_valid(self):
        attestations, errors, manual_count = _collect_review_attestations(
            attest_asked_full_name="I asked Jane Doe for their full name and scanned the export for Jane Doe.",
            attest_asked_sensitive=(
                "I asked about company, client, and internal names plus URLs; "
                "none were sensitive and no extra redactions were needed."
            ),
            attest_manual_scan="I performed a manual scan and reviewed 20 sessions across beginning, middle, and end.",
            full_name="Jane Doe",
        )
        assert not errors
        assert manual_count == 20
        assert "Jane Doe" in attestations["asked_full_name"]

    def test_collect_review_attestations_invalid(self):
        _attestations, errors, manual_count = _collect_review_attestations(
            attest_asked_full_name="scanned quickly",
            attest_asked_sensitive="checked stuff",
            attest_manual_scan="manual scan of 5 sessions",
            full_name="Jane Doe",
        )
        assert errors
        assert "asked_full_name" in errors
        assert "asked_sensitive_entities" in errors
        assert "manual_scan_done" in errors
        assert manual_count == 5

    def test_collect_review_attestations_skip_full_name_valid(self):
        _attestations, errors, manual_count = _collect_review_attestations(
            attest_asked_full_name="User declined to share full name; skipped exact-name scan.",
            attest_asked_sensitive=(
                "I asked about company/client/internal names and private URLs; "
                "none were sensitive and no extra redactions were needed."
            ),
            attest_manual_scan="I performed a manual scan and reviewed 20 sessions across beginning, middle, and end.",
            full_name=None,
            skip_full_name_scan=True,
        )
        assert not errors
        assert manual_count == 20

    def test_collect_review_attestations_skip_full_name_invalid(self):
        _attestations, errors, _manual_count = _collect_review_attestations(
            attest_asked_full_name="Asked user and scanned it.",
            attest_asked_sensitive="I asked about company/client/internal names and private URLs; none found.",
            attest_manual_scan="I performed a manual scan and reviewed 20 sessions across beginning, middle, and end.",
            full_name=None,
            skip_full_name_scan=True,
        )
        assert "asked_full_name" in errors

    def test_validate_publish_attestation(self):
        _normalized, err = _validate_publish_attestation("User explicitly approved publishing this dataset now.")
        assert err is None

        _normalized, err = _validate_publish_attestation("ok to go")
        assert err is not None

    def test_scan_for_text_occurrences(self, tmp_path):
        f = tmp_path / "sample.jsonl"
        f.write_text('{"message":"Jane Doe says hi"}\n{"message":"nothing here"}\n')
        result = _scan_for_text_occurrences(f, "Jane Doe")
        assert result["match_count"] == 1


class TestScanHighEntropyStrings:
    def test_detects_real_secret(self):
        secret = "aB3dE6gH9jK2mN5pQ8rS1tU4wX7yZ0c"
        content = f"some config here token {secret} and more text"
        results = _scan_high_entropy_strings(content)
        assert len(results) >= 1
        assert any(result["match"] == secret for result in results)
        for result in results:
            if result["match"] == secret:
                assert result["entropy"] >= 4.0

    def test_filters_uuid(self):
        content = "id=550e8400e29b41d4a716446655440000 done"
        results = _scan_high_entropy_strings(content)
        assert not any("550e8400" in result["match"] for result in results)

    def test_filters_uuid_with_hyphens(self):
        content = "id=550e8400-e29b-41d4-a716-446655440000 done"
        results = _scan_high_entropy_strings(content)
        assert not any("550e8400" in result["match"] for result in results)

    def test_filters_hex_hash(self):
        content = "commit=abcdef1234567890abcdef1234567890abcdef12 done"
        results = _scan_high_entropy_strings(content)
        assert not any("abcdef1234567890" in result["match"] for result in results)

    def test_filters_known_prefix_eyj(self):
        content = "token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 done"
        results = _scan_high_entropy_strings(content)
        assert not any(result["match"].startswith("eyJ") for result in results)

    def test_filters_known_prefix_ghp(self):
        content = "token=ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345 done"
        results = _scan_high_entropy_strings(content)
        assert not any(result["match"].startswith("ghp_") for result in results)

    def test_filters_file_extension_path(self):
        content = "import=some_long_module_name_thing.py done"
        results = _scan_high_entropy_strings(content)
        assert not any(".py" in result["match"] for result in results)

    def test_filters_path_like(self):
        content = "path=src/components/authentication/LoginForm done"
        results = _scan_high_entropy_strings(content)
        assert not any("src/components" in result["match"] for result in results)

    def test_filters_low_entropy(self):
        content = "val=aaaaaaBBBBBB111111aaaaaaBBBBBB111111 done"
        results = _scan_high_entropy_strings(content)
        assert not any("aaaaaa" in result["match"] for result in results)

    def test_filters_no_mixed_chars(self):
        content = "val=abcdefghijklmnopqrstuvwxyz done"
        results = _scan_high_entropy_strings(content)
        assert not any("abcdefghijklmnop" in result["match"] for result in results)

    def test_context_snippet(self):
        secret = "aB3dE6gH9jK2mN5pQ8rS1tU4wX7yZ0c"
        content = "before_context " + secret + " after_context"
        results = _scan_high_entropy_strings(content)
        matched = [result for result in results if result["match"] == secret]
        assert len(matched) == 1
        assert "before_context" in matched[0]["context"]
        assert "after_context" in matched[0]["context"]

    def test_results_capped_at_max(self):
        import random
        import string

        rng = random.Random(42)
        chars = string.ascii_letters + string.digits
        secrets = ["".join(rng.choices(chars, k=30)) for _ in range(25)]
        content = " ".join(f"key={secret}" for secret in secrets)
        results = _scan_high_entropy_strings(content, max_results=15)
        assert len(results) <= 15

    def test_empty_content(self):
        assert _scan_high_entropy_strings("") == []

    def test_sorted_by_entropy_descending(self):
        secret1 = "aB3dE6gH9jK2mN5pQ8rS1tU4wX7yZ0c"
        secret2 = "Zx9Yw8Xv7Wu6Ts5Rq4Po3Nm2Lk1Jh0G"
        content = f"a={secret1} b={secret2}"
        results = _scan_high_entropy_strings(content)
        if len(results) >= 2:
            assert results[0]["entropy"] >= results[1]["entropy"]

    def test_filters_benign_prefix_https(self):
        content = "url=https://example.com/some/long/path/here done"
        results = _scan_high_entropy_strings(content)
        assert not any(result["match"].startswith("https://") for result in results)

    def test_filters_three_dots(self):
        content = "ver=com.example.app.module.v1.2.3 done"
        results = _scan_high_entropy_strings(content)
        assert not any("com.example.app" in result["match"] for result in results)

    def test_filters_node_modules(self):
        content = "path=some_long_node_modules_path_thing done"
        results = _scan_high_entropy_strings(content)
        assert not any("node_modules" in result["match"] for result in results)

    def test_filters_very_large_base64_like_blob(self):
        blob = "AbC123+/" * 700
        results = _scan_high_entropy_strings(f"payload={blob}")
        assert not any(result["match"] == blob for result in results)


class TestScanPiiHighEntropy:
    def test_includes_high_entropy_when_present(self, tmp_path):
        secret = "aB3dE6gH9jK2mN5pQ8rS1tU4wX7yZ0c"
        f = tmp_path / "export.jsonl"
        f.write_text(f'{{"message": "config token {secret} end"}}\n')
        results = _scan_pii(f)
        assert "high_entropy_strings" in results
        assert any(result["match"] == secret for result in results["high_entropy_strings"])

    def test_excludes_high_entropy_when_clean(self, tmp_path):
        f = tmp_path / "export.jsonl"
        f.write_text('{"message": "nothing suspicious here at all"}\n')
        results = _scan_pii(f)
        assert "high_entropy_strings" not in results

    def test_streams_blob_heavy_exports_without_read_text(self, tmp_path, monkeypatch):
        blob = "AbC123+/" * (2 * 1024 * 1024 // 8)
        f = tmp_path / "export.jsonl"
        f.write_text(
            f'{{"message": "{blob}"}}\n{{"message": "contact jane@example.com"}}\n',
            encoding="utf-8",
        )

        def fail_read_text(*args, **kwargs):
            raise AssertionError("Path.read_text should not be called")

        monkeypatch.setattr("pathlib.Path.read_text", fail_read_text)

        results = _scan_pii(f)

        assert results["emails"] == ["jane@example.com"]
        assert "high_entropy_strings" not in results


class TestConfirmStreaming:
    def test_scan_export_review_parallel_matches_serial(self, tmp_path, monkeypatch):
        export_file = tmp_path / "export.jsonl"
        export_file.write_text(
            "".join(
                [
                    '{"project":"proj-a","model":"model-a","message":"Jane Doe","messages":[]}\n',
                    '{"project":"proj-b","model":"model-b","message":"contact jane@example.com","messages":[]}\n',
                    '{"project":"proj-a","model":"model-a","message":"token aB3dE6gH9jK2mN5pQ8rS1tU4wX7yZ0c","messages":[]}\n',
                    '{"project":"proj-c","model":"model-c","message":"Jane Doe again","messages":[]}\n',
                ]
            ),
            encoding="utf-8",
        )

        class FakeExecutor:
            def __init__(self, max_workers):
                self.max_workers = max_workers

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def map(self, fn, payloads, chunksize=1):
                del chunksize
                return [fn(payload) for payload in payloads]

        monkeypatch.setattr("dataclaw._cli.review.ProcessPoolExecutor", FakeExecutor)

        serial = _scan_export_review(export_file, full_name_query="Jane Doe", workers=1)
        parallel = _scan_export_review(export_file, full_name_query="Jane Doe", workers=2)

        assert parallel == serial

    def test_confirm_reviews_export_in_single_pass(self, tmp_path, monkeypatch, capsys):
        export_file = tmp_path / "export.jsonl"
        export_file.write_text(
            '{"project":"proj-a","model":"model-a","message":"Jane Doe","messages":[]}\n'
            '{"project":"proj-b","model":"model-b","message":"contact jane@example.com","messages":[]}\n',
            encoding="utf-8",
        )
        perf_counter_values = iter([10.0, 11.5])

        open_calls = 0
        real_open = open

        def counting_open(file, *args, **kwargs):
            nonlocal open_calls
            if str(file) == str(export_file):
                open_calls += 1
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr("builtins.open", counting_open)
        monkeypatch.setattr("dataclaw._cli.review.time.perf_counter", lambda: next(perf_counter_values))

        saved_config = {}
        confirm(
            file_path=export_file,
            full_name="Jane Doe",
            attest_asked_full_name="I asked Jane Doe for their full name and scanned the export for Jane Doe.",
            attest_asked_sensitive=(
                "I asked about company, client, and internal names plus URLs; "
                "none were sensitive and no extra redactions were needed."
            ),
            attest_manual_scan="I performed a manual scan and reviewed 20 sessions across beginning, middle, and end.",
            load_config_fn=lambda: {},
            save_config_fn=lambda cfg: saved_config.update(cfg),
        )

        payload = json.loads(capsys.readouterr().out)
        assert payload["elapsed"] == "1.50s"
        assert payload["total_sessions"] == 2
        assert payload["full_name_scan"]["match_count"] == 1
        assert payload["pii_scan"]["emails"] == ["jane@example.com"]
        assert open_calls == 1
        assert saved_config["stage"] == "confirmed"
