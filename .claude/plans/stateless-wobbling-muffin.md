# Training Value Scoring: Intent, Outcome, Taste

## Context

The current badge system (`badges.py`) has surface-level heuristics — it detects keyword presence but doesn't assess **quality of signal for post-training**. Per the Ground Foundry memo, trace value comes from three pillars:

- **Intent**: The gap between what humans ask for and what they actually want
- **Outcome**: Verification that it actually worked — every failure becomes a training sample
- **Taste**: Subjective human judgment when multiple outputs are correct but humans prefer one

We need a `compute_training_value()` scoring system that produces pillar scores (0-1), detected signal names, a composite score, and a recommendation label. All heuristic-based (no LLM calls), runs locally during scanning.

---

## Change 1: Scoring engine — `dataclaw/badges.py`

### New function: `compute_training_value(session, badges=None) -> dict`

Returns:
```python
{
    "intent_score": float,       # 0-1
    "intent_signals": list[str],
    "outcome_score": float,      # 0-1
    "outcome_signals": list[str],
    "taste_score": float,        # 0-1
    "taste_signals": list[str],
    "antipattern_penalty": float,# 0-1
    "antipatterns": list[str],
    "training_value": float,     # 0-1 composite
    "recommendation": str,       # "high_value"|"recommended"|"standard"|"low_value"|"skip"
}
```

### Intent signals (weight 0.25 of composite)

| Signal | Weight | Detection |
|--------|--------|-----------|
| `multi_constraint` | 0.15 | Numbered/bullet lists or sequencing words ("first", "then", "also") in first 3 user msgs. Fire if 3+ matches. |
| `intent_refinement` | 0.20 | User refines mid-conversation: "actually", "wait", "instead", "no I mean", "let me clarify", "scratch that". Scan all user msgs. |
| `domain_specificity` | 0.15 | Domain vocabulary density beyond generic coding — specialized terms (API, middleware, schema, mutex, B-tree, sharding, WASM, FFI, etc.) + reuse existing `_SCIENTIFIC_LIBS`/`_SCIENTIFIC_TERMS`. Count unique domain terms. |
| `architectural_scope` | 0.15 | Multi-file/cross-cutting work. Fire if unique file extensions >= 3 OR unique directory prefixes >= 4 in `files_touched`. |
| `prompt_richness` | 0.15 | Average word count of first 3 user messages. Caps at 80+ words avg. |
| `multi_turn_depth` | 0.20 | `user_messages` count. 1 msg = 0, 11+ = max contribution. |

### Outcome signals (weight 0.40 of composite) — highest weight per memo

| Signal | Weight | Detection |
|--------|--------|-----------|
| `verified_success` | 0.20 | Reuse existing `outcome_badge`: tests_passed=1.0, unknown+writes=0.3, analysis_only=0.1, failed=0.0 |
| `error_recovery_arc` | 0.30 | **State machine** walking messages in order: IDLE→ERROR_SEEN (error in tool output)→FIX_ATTEMPTED (Write/Edit tool or fix keywords in assistant)→ARC_COMPLETE (verify keywords in tool output). Count completed arcs. `min(arc_count/3, 1.0)`. |
| `iterative_fix_cycles` | 0.15 | Count error→fix transitions (even without verification). `min(fix_attempts/4, 1.0)`. |
| `multi_verification` | 0.10 | Distinct verification methods used: test runners, linters, build tools, type checkers. `min(method_count/3, 1.0)`. |
| `task_completion` | 0.15 | Last 3 user messages contain completion signals ("thanks", "looks good", "that works", "ship it"). Abandonment signals ("never mind", "forget it") apply negative. |
| `exit_code_success` | 0.10 | Explicit `exit code 0` or successful command in final third. |

### Taste signals (weight 0.35 of composite) — rare and valuable for RLHF

| Signal | Weight | Detection |
|--------|--------|-----------|
| `explicit_correction` | 0.30 | User says "no, instead", "that's not what I", "please don't", "wrong approach". `min(count/3, 1.0)`. |
| `style_preference` | 0.20 | "too verbose", "simpler", "more readable", "prefer camelCase", "keep it DRY", "let's use a different". `min(count/3, 1.0)`. |
| `approach_rejection` | 0.20 | "let's not", "let's try a different", "go back to", "revert", "can we use another". `min(count/2, 1.0)`. |
| `quality_feedback` | 0.15 | Positive ("good", "perfect", "much better") + negative ("ugly", "hacky", "too complex") feedback from user. Both are valuable. `min(total/4, 1.0)`. |
| `refinement_of_working` | 0.15 | Post-verification user changes — after tests pass, user still requests modifications (shows taste beyond correctness). |

### Anti-patterns (subtracted from composite)

| Pattern | Penalty | Detection |
|---------|---------|-----------|
| `thrashing` | 0.30 | Same error (first line, numbers normalized) appears 3+ times. `min(repeat_count/5, 1.0)`. |
| `abandoned` | 0.25 | Last 3 tool outputs contain errors with no subsequent verification. |
| `trivial_session` | 0.25 | Single user msg OR total tokens < 500 OR no tools + < 3 messages. |
| `hallucination_pattern` | 0.20 | Read tool returns "file not found" / "does not exist" errors. `min(count/4, 1.0)`. |

### Composite formula

```python
raw = intent_score * 0.25 + outcome_score * 0.40 + taste_score * 0.35
training_value = max(raw - penalty, 0.0)
```

### Recommendation thresholds

| Label | Threshold | Meaning |
|-------|-----------|---------|
| `high_value` | >= 0.65 | Multiple pillars firing. Auto-shortlist candidate. |
| `recommended` | >= 0.45 | At least one strong pillar. Good training data. |
| `standard` | >= 0.25 | Some signals but nothing exceptional. |
| `low_value` | >= 0.10 | Minimal signal. Trivial or unverified. |
| `skip` | < 0.10 | Anti-patterns dominate. |

### Integration

Update `compute_all_badges()` to call `compute_training_value()` and merge the result dict. Add `_get_assistant_messages()` helper.

---

## Change 2: Database schema — `dataclaw/index.py`

### New columns (via ALTER TABLE migration)

```sql
intent_score        REAL DEFAULT 0.0,
intent_signals      TEXT,           -- JSON array
outcome_score       REAL DEFAULT 0.0,
outcome_signals     TEXT,           -- JSON array
taste_score         REAL DEFAULT 0.0,
taste_signals       TEXT,           -- JSON array
antipattern_penalty REAL DEFAULT 0.0,
antipatterns        TEXT,           -- JSON array
training_value      REAL DEFAULT 0.0,
recommendation      TEXT DEFAULT 'standard',
```

### Changes

- Add `_migrate_training_value(conn)` — idempotent ALTER TABLE migration called from `open_index()`
- Add indexes on `training_value` and `recommendation`
- Update `upsert_sessions()` INSERT to include new columns
- Add `training_value`, `recommendation` to `allowed_sort_columns` in `query_sessions()`
- Add `by_recommendation` and `training_value_by_source` to `get_dashboard_analytics()`

---

## Change 3: API layer — `dataclaw/daemon.py`

- Add `intent_signals`, `outcome_signals`, `taste_signals`, `antipatterns` to `_parse_json_fields()` JSON array parsing

---

## Change 4: Frontend types — `types.ts`

Add to `Session`:
```typescript
intent_score: number;
intent_signals: string[];
outcome_score: number;
outcome_signals: string[];
taste_score: number;
taste_signals: string[];
antipattern_penalty: number;
antipatterns: string[];
training_value: number;
recommendation: string;
```

Add to `DashboardData`:
```typescript
by_recommendation: { recommendation: string; count: number }[];
training_value_by_source: { source: string; avg_training_value: number; count: number }[];
```

---

## Change 5: BadgeChip — `BadgeChip.tsx`

Add `recommendation` badge kind with colors:
- `high_value` → green, `recommended` → blue, `standard` → gray, `low_value` → amber, `skip` → red

Add labels for all signal names (e.g., `error_recovery_arc` → "Error Recovery").

---

## Change 6: TraceCard — `TraceCard.tsx`

Add between meta row and badges: training value mini-bar (60px wide, 4px tall, color-coded) + score percentage + recommendation badge chip.

---

## Change 7: FilterBar — `FilterBar.tsx`

Add sort option: `<option value="training_value:desc">Recommended</option>` as first option.

---

## Change 8: SessionDetail — `SessionDetail.tsx`

Add TRAINING VALUE section in left panel (between BADGES and SENSITIVITY):
- Composite score bar + recommendation badge
- Three `PillarBar` sub-components showing label, score bar, and signal chips for intent/outcome/taste
- Anti-patterns section (conditional, red-themed)

New `PillarBar` component: label + score + mini progress bar + wrapped signal name chips.

---

## Change 9: Dashboard — `Dashboard.tsx`

Add two sections:
1. **Recommendation Distribution** — horizontal bar chart by recommendation label
2. **Training Value by Source** — bar chart showing avg training value per source

---

## Change 10: Tests — `tests/test_badges.py`

New `TestTrainingValue` class with cases:
- `test_high_value_session` — error recovery + user corrections → high_value
- `test_trivial_session` — single message → skip
- `test_thrashing_session` — repeated errors → penalty
- `test_intent_multi_constraint` — numbered list → signal fires
- `test_taste_correction` — "no, instead" → signal fires
- `test_error_recovery_arc` — error→fix→verify state machine
- `test_all_fields_present` — result dict completeness
- `test_scores_in_range` — all scores 0-1
- `test_recommendation_labels` — valid label values

Add `_make_rich_session()` helper for building multi-turn test sessions with custom user messages and tool outputs.

Also add test in `tests/test_index.py` for schema migration on existing databases.

---

## Verification

1. `pytest tests/test_badges.py -v` — all new training value tests pass
2. `pytest tests/ -v` — all existing tests still pass
3. `npx tsc --noEmit` — no type errors in frontend
4. `npx vite build` — builds successfully
5. `dataclaw scan` — rescans and populates training_value for all sessions
6. Browser: Inbox shows training value bars, "Recommended" sort works
7. Browser: SessionDetail shows pillar breakdown with signal chips
8. Browser: Dashboard shows recommendation distribution

---

## Implementation order

1. `badges.py` — scoring engine (largest change, ~300 lines)
2. `index.py` — schema + queries
3. `daemon.py` — JSON field parsing
4. `tests/test_badges.py` — test coverage
5. `types.ts` — TypeScript interfaces
6. `BadgeChip.tsx` — recommendation badge
7. `TraceCard.tsx` — training value display
8. `FilterBar.tsx` — sort option
9. `SessionDetail.tsx` — pillar breakdown
10. `Dashboard.tsx` — analytics charts
