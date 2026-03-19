---
name: dataclaw-score
description: >
  Score coding agent sessions for quality. Use when the user asks to score a
  session, score sessions, re-score, auto-score, batch score, or evaluate traces.
disable-model-invocation: true
allowed-tools: Bash(dataclaw *)
argument-hint: "<session-id>"
---

# Score Sessions

## Quick Path: Batch Auto-Score

If no session ID was provided, suggest the automated approach first:

```bash
# Score all unscored sessions automatically (recommended)
dataclaw score --batch --auto-triage --limit 20

# Or without auto-triage:
dataclaw score --batch --limit 20
```

For hands-on scoring of a specific session, continue below.

## Session Data

!`dataclaw score-view $ARGUMENTS 2>/dev/null || echo "Usage: /dataclaw-score <session-id> — or run without args for batch scoring guidance. Run 'dataclaw score-batch --limit 10' to get session IDs."`

## Scoring Rubric (1-5)

**5 = Excellent** — Clear non-trivial coding task. Successful verified outcome (tests pass, code compiles). Rich tool usage with multi-step problem-solving. Demonstrates patterns worth learning from.

**4 = Good** — Clear task with useful outcome. Some tool usage and verification. Reasonable conversation quality.

**3 = Average** — Understandable but routine task. Partial or unverified outcome. Basic conversation with limited tool usage.

**2 = Low** — Vague or trivial task. Failed/unclear outcome. Minimal meaningful interaction.

**1 = Poor** — No discernible coding task. Trivially short or broken session. Zero training data value.

### Evaluation dimensions
- **INTENT**: Is there a clear coding task? Would a reader understand the goal?
- **OUTCOME**: Did the task succeed? Were results verified (tests, build, manual check)?
- **SUBSTANCE**: Enough back-and-forth? Meaningful tool usage? Not trivial?
- **AGENT QUALITY**: Reasonable approaches? Good tool choices? Handles errors well?

### Source-specific guidance
**Claude Code**: Value IDE-like workflows (reading code → editing → running tests). Bash tool usage (tests, builds, git) adds significant value. Multi-file changes more interesting than single edits. Debugging sessions with clear resolution are highly valuable.

**Codex**: Value clear task specifications and structured implementation. Multi-step implementations more interesting than simple completions.

## Your Task

Score this session 1-5 based on the rubric above. Then store the score:

```bash
dataclaw set-score <session-id> --quality <score> --reason "<1-2 sentence explanation>"
```
