# DataClaw reliability: merge-on-upload + privacy filter — design & plan

> Produced from a multi-subagent audit (2026-05-30). Every claim is cited file:line.
> Two goals: (1) uploads must **merge**, never destroy, prior data; (2) the **model
> privacy filter** the UI promises must actually run.

---

## Part A — Incremental / merge-on-upload

### Current behavior (verified)
Upload is a **wholesale overwrite**. `push_to_huggingface` (`dataclaw/_cli/exporting.py:651`)
calls `api.upload_file(path_in_repo="conversations.jsonl", ...)` (`:669`) with the locally
re-generated full file. No download, no merge. The remote dataset always equals the latest
local scan, so any session not reproducible locally **right now** is silently deleted
(second machine, log rotation, narrower `--source`, new excluded project, cleared `~/.claude`).

### Intent residue found
- `last_export_cutoff` — written by Rust (`app/src-tauri/src/dataclaw.rs:239-261`), **read by
  nobody**. Half-built incremental-cutoff scaffold.
- `dataclaw/jsonl_tools.py` — has `IDENTITY_FIELDS=("source","project","session_id","start_time")`
  (`:26`), `identity_key()` (`:221`), `index_jsonl()` (`:239`), `diff_jsonl_files()` (`:685`).
  **No merge function; not wired to upload** — `diff-jsonl` is an offline CLI command only.
- Data-safety intent was actually shipped as **trust gates** (session-shrink `review.py:616`,
  redaction-drift `commands.py:780`), all comparing to **local** `last_export` — not remote.

### The correct unit
Each JSONL line is one **session** record with a `messages` array. "New messages since last
upload" = same `identity_key`, more messages, different record hash. The merge unit is the
**session**, not the message.

### Recommended design: download → merge (union) → re-redact → re-gate → reupload

1. **Download** prior remote `conversations.jsonl` via `hf_hub_download(repo_id,
   "conversations.jsonl", repo_type="dataset")`. **Fail closed**: only a confirmed 404
   (`EntryNotFoundError`) or missing-repo (`RepositoryNotFoundError`) counts as "empty remote";
   any network/auth/HTTP error **aborts the push** (never overwrite remote with local-only).
2. **Union by `identity_key`** (read records RAW, not via `index_jsonl` — that normalizes and
   strips `originalFile`, see H3):
   - remote-only → carry forward (never drop)
   - local-only → add
   - both → keep superset (more messages; tie-break larger bytes, then later `end_time`)
   - **Invariant `merged_total >= remote_total`** ⇒ shrink is impossible by construction.
3. **🔴 RE-REDACT carried-forward remote records through the CURRENT redactor** before writing
   them (see "Critical correctness requirement" below). This is mandatory, not optional.
4. **Re-run the confirm gates on the MERGED file and re-hash it** so reviewed-bytes ==
   shipped-bytes (preserve the SHA invariant at `commands.py:365-376`).
5. **Upload the merged union** (not the local export) with `parent_commit=<repo head sha>`
   for optimistic concurrency; on 412 conflict re-download+re-merge (cap ~5 attempts).
   Update `meta["sessions"]`/`last_export.sessions` to `merged_total` so metadata and the
   shrink gate stay consistent (H5).

Hook point: `push_to_huggingface` upload block (`exporting.py:666-675`). Merge fn belongs in
`jsonl_tools.py` next to `identity_key`/`index_jsonl`. Draft code exists in the agent transcript.

### 🔴 Critical correctness requirement (the naive merge is a PRIVACY REGRESSION)
Remote records were redacted under **whatever policy was current when they were uploaded**.
Carrying them forward verbatim **re-publishes old, looser-policy redaction** and **bypasses
every gate** (gates run in `confirm()` on the local file, before the merge). Adversarial review
verdict on the naive union: scenarios (a) new redact_string, (b) superset-by-count picking the
older copy, (c) future model filter, (d) gate bypass → **leak, leak, leak, total-bypass**.

**Fix:** re-run `secrets.transform_session` (+ `Anonymizer`) over every carried-forward record
with the **current** config before reupload. This is **idempotent and pseudonym-stable**
(verified): secrets become the constant `[REDACTED]` which matches no pattern; pseudonyms are
deterministic `user_+sha256(name)[:8]` and the hash token doesn't re-match the username regex.
Then re-gate + re-hash the merged file.

### Additional holes the merge must handle
- **H1 `start_time` key drift** (`jsonl_tools.py:26`, `parsers/common.py:157-164`): format drift
  (`Z` vs `+00:00`, `None` vs backfilled) → same session under two keys → **duplicate + leak**.
  Fix: canonicalize `start_time` before keying, or key on `(source, project, session_id)`.
- **H2 anonymized `project` in key** (`secrets.py:572-576`): `project` is anonymized in-place and
  is part of the key → key drift. Strip `project` from key or key on post-anon identity.
- **H3 `normalize_for_diff` strips `originalFile`** (`jsonl_tools.py:234-235`): don't read remote
  records via the diff-normalizing loader or you destroy `originalFile` content.
- **H4 schema drift**: old remote records may lack fields the current walker expects → missed
  secrets or crash. Re-redaction must be schema-tolerant.
- **H5 non-atomic upload + stale `last_export.sessions`**: 3 separate `upload_file` commits;
  update session count to merged total.

### Trust gates
Keep the existing **local** gates unchanged (they guard the human confirm step). Do **not** add a
remote-shrink gate — the union invariant makes remote shrink impossible. But the merged file
**must** pass the gates after merge+re-redaction (step 4).

---

## Part B — Model-based privacy filter

### It was real, and was lost (not reverted)
A complete **848-line `dataclaw/privacy_filter.py`** + `tests/test_privacy_filter.py` was
committed on `5d0a741` ("Add macOS app release flow", a side branch) and **never merged to main**.
A *different* same-titled commit (`42b21d1`) landed on main without it. The file was on disk as
late as the 2026-05-16 desloppify scan, then lost untracked. The `pii` deps
(`transformers`, `torch`, `accelerate`, `tokenizers`) were dropped from HEAD `pyproject.toml`.

Recover with: `git show 5d0a741:dataclaw/privacy_filter.py` and `:tests/test_privacy_filter.py`.

### What it does (and quality)
`transformers.pipeline(task="token-classification", aggregation_strategy="simple")` NER with real
CPU/MPS device handling (`resolve_device`/`_auto_device`), 480-token chunking, min_score 0.85,
oversized-session blanket-redact guard, reverse-order span splicing. **Core logic is sound.**

Bugs / gaps (line refs on the `5d0a741` version):
- 🔴 **Field-walk mismatch (must fix):** it walks `messages[].content/thinking` + `tool_uses[].input/output`
  but **NOT `messages[].content_parts`**, which HEAD's `secrets.transform_session` *does* walk
  (`secrets.py:590`). PII in `content_parts` would pass untouched. Add `content_parts` to the
  field loops in `scan_session`/`redact_session`/`_redact_oversized_session` + a test.
- 🔴 **`MODEL_ID = "openai/privacy-filter"` is a placeholder** — does not exist on the Hub.
- Minor: cross-chunk-boundary entities may be split/under-redacted (low severity); `min_score`
  discarded in `_load` then re-applied in `scan_text` (cosmetic); `dtype=` kwarg needs transformers ≥4.45.

### Model decision (drop-in for the existing pipeline call)
- **Default: `openai/privacy-filter`** — the model the original implementation
  intended. It is a REAL, published model (Apache-2.0, ~300k downloads,
  token-classification, safetensors + ONNX) — the earlier "placeholder that doesn't
  exist" claim was WRONG. It drops into the existing
  `pipeline(task="token-classification", aggregation_strategy="simple")` call with no
  adapter code and detects PII as intended.
- Overridable via the `privacy_filter.model` config key / `DATACLAW_PRIVACY_FILTER_MODEL`
  env var, so swapping is trivial.
- Alternatives if ever needed: `lakshyakh93/deberta_finetuned_pii` (MIT, broad labels,
  pure drop-in). Avoid GLiNER / Presidio (need adapters / heavier deps).

### Rewiring against HEAD `_cli` structure
- **Mutation happens only at EXPORT time**, inline per session, right after
  `secrets.transform_session` (`exporting.py:201` parallel worker, `:292` serial). Confirm
  (`review.py:625`) is **read-only** and hashes the file (`review.py:830`); publish enforces that
  hash (`commands.py:365-376`). **So the model edits MUST run at export, before the hash lock** —
  not at confirm.
- Keep the dict/text functions (`redact_session`, `redact_text`, `scan_text`, `_load`,
  `resolve_device`, oversized guard); **discard** the shard/jsonl/manifest layer (HEAD has no
  run_dir/manifest pipeline — it was dropped).
- Read config (free-form dict, no schema in `config.py`):
  `privacy_filter.{enabled (default False), device, min_score=0.85, include_tool_io=True, roles={user}}`.
  Rust already writes `privacy_filter.enabled/.device` (`dataclaw.rs:571,580`) — this closes the gap.
- Emit `privacy_filter_*` events via `progress_callback` → lights up the **already-present** dead
  Dashboard `model_privacy` stage (`Dashboard.tsx:209-210`, handlers `:434-516`). `mechanical_pii`
  stage maps to the existing regex layer.

### Deps / packaging
- Restore `[project.optional-dependencies] pii = ["transformers>=4.57","torch>=2.3","accelerate","tokenizers"]`
  + the `pii` pytest marker. Keep as an **extra** so the lazy-import graceful-degradation holds.
- **Do NOT bundle torch into the Mac sidecar by default** (~1.5–2.5 GB; `pyinstaller.spec` has no
  torch hooks). Default `enabled=false`. Treat the model stage as power-user / `dataclaw[pii]`, or
  a separate heavy build. **Download the model at first run** (HF cache), don't bundle weights.
- **Graceful degradation:** if enabled but torch/model/network unavailable → **warn and continue
  with mechanical-only redaction**, do not abort the export.

---

## Prioritized action list
1. **Merge-on-upload with re-redaction + re-gate** (Part A). The data-destruction bug *and* the
   privacy regression it would introduce — do them together; one is unsafe without the other.
2. **Atomic export write** (temp+fsync+rename in `exporting.py:493`) — independent, stops silently
   truncated datasets being published. (From the first audit.)
3. **Privacy filter recovery + content_parts fix + real MODEL_ID + export-time rewire** (Part B).
4. **Source enum reconcile** (drop `hermes`, add `cursor`, validate in Rust, fail-closed in Python)
   and **harden secret patterns + salt the anonymizer**. (From the first audit.)
5. Real cross-process run lock; batch the 3 HF uploads into one `create_commit`.

## Open verification items (couldn't run live)
- `huggingface_hub` version: confirm `upload_file(parent_commit=...)` support and that conflicts
  surface as HTTP 412; confirm `hf_hub_download` error classes.
- Live Hub availability + offset behavior of the two recommended `MODEL_ID`s.
