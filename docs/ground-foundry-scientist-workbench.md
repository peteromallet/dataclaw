# Ground Foundry Scientist Workbench

## Why this exists

The seed memo frames Ground Foundry as the neutral infrastructure layer that captures authentic human+AI work traces, refines them, and delivers them downstream. `dataclaw` already covers the first part of that pipeline for coding agents: it can discover Claude Code, Codex, OpenClaw, Gemini CLI, and OpenCode sessions, normalize them, redact obvious secrets, and gate export behind a review flow.

What is still missing is the scientist-facing product:

- continuous local trace tracking instead of one-off export
- a review surface that makes sessions legible and searchable
- explicit curation so scientists choose what to share
- upload to Ground Foundry infrastructure, not a personal Hugging Face repo
- provenance and consent records that make each uploaded bundle defensible

This document proposes that product.

## Product thesis

Scientists will share traces if the product behaves like a local research notebook, not a data extraction tool.

That implies five constraints:

1. Local-first. Raw traces stay on the scientist's machine until they explicitly review and submit them.
2. Passive collection. The system should automatically ingest traces from Claude Code, Codex, and OpenClaw without changing the scientist's workflow.
3. Review before upload. Upload is always a second step after filtering, sampling, redaction review, and explicit selection.
4. Trace usefulness must be visible. The scientist should see why a session is valuable: task type, domain, outcome signal, tool usage, and novelty.
5. Provenance must be auditable. Every upload needs a clear chain from raw session to redacted bundle to server receipt.

## UX stance

The right mental model is not "export conversations." It is "curate research traces."

That changes the UI in three important ways:

- the default object is a trace card with usefulness and risk signals, not a raw transcript blob
- search must work across projects, goals, tools, and outcomes, not only keyword matches
- upload should feel like assembling an evidence-backed submission, not clicking "sync"

For this audience, a friendly UI means low cognitive overhead during triage and high control during review.

## User workflow

### 1. Connect sources

The scientist installs a lightweight desktop app or local daemon.

It detects:

- Claude Code sessions from `~/.claude/projects`
- Codex sessions from `~/.codex/sessions` and archived sessions
- OpenClaw sessions from `~/.openclaw/agents`

The app continuously indexes new sessions into a local store.

### 2. Track work automatically

As the scientist works, the app creates a local catalog:

- project / repo
- source tool
- timestamps and duration
- model used
- tool calls and files touched
- inferred task tags
- outcome hints such as tests run, build pass/fail, notebook execution, repo changes

This creates a living inbox of traces instead of a one-time dump.

### 3. Review and curate

The scientist opens a review UI with three queues:

- `New`: newly indexed sessions not yet reviewed
- `Shortlist`: sessions marked as potentially shareable
- `Ready`: sessions approved for upload

Review happens at three levels:

- Session level: inspect transcript, tools, files, redactions, outcome summary
- Span level: keep only selected turns or redact selected spans
- Batch level: select many sessions by project, tag, source, or date range

### 4. Approve upload

Before upload, the scientist sees a bundle preview:

- number of sessions
- projects represented
- detected sensitive entities
- manual redactions added
- license / consent language
- destination dataset or program

They then approve upload with an explicit attestation.

### 5. Ground Foundry ingestion

The upload service receives a signed bundle containing:

- normalized sessions
- redaction manifest
- provenance metadata
- contributor attestation
- derived quality signals

The server acknowledges receipt and assigns processing status for downstream refinement.

## Recommended product shape

For MVP, the best shape is a local web app backed by a local daemon.

Why this is the right choice:

- a CLI is too weak for span-level redaction, transcript review, and bulk curation
- a full desktop app is heavier than needed for the first release
- a local web app preserves the local-first trust model while shipping much faster

Recommended split:

- `dataclawd`: local background service for indexing, storage, search, bundling, and upload
- local web app: browser-based UI served on `localhost`
- optional desktop wrapper later: Tauri or Electron only after the workflow stabilizes

This keeps the current Python codebase useful. The parsers remain in Python, the daemon can also be Python, and only the review UI needs a frontend stack.

## Product surface

### A. Local collector

This is an extension of current `dataclaw` parsing.

Responsibilities:

- watch supported session directories
- parse sessions incrementally
- normalize into one internal schema
- maintain a local index for fast search and filtering
- never upload automatically

Recommended implementation:

- keep current parsers in `dataclaw/parser.py`
- add incremental ingestion with a local SQLite database
- store normalized session metadata separately from raw transcript blobs

Suggested new commands:

- `dataclaw watch`
- `dataclaw inbox`
- `dataclaw review`
- `dataclaw bundle`
- `dataclaw publish`
- `dataclaw serve`

### B. Scientist review app

This should be the primary user experience.

### Information architecture

Recommended left navigation:

- `Inbox`
- `Search`
- `Bundles`
- `Policies`
- `Uploads`
- `Settings`

This gives the product six distinct jobs:

- discover new traces
- find important old traces
- inspect and redact
- assemble submissions
- track what was already uploaded
- control privacy and source settings

### Core views

- Inbox: recent traces with filters for source, repo, date, outcome, sensitive-risk score
- Session detail: transcript, tools, file diff summary, test/output summary, redaction highlights
- Compare view: cluster similar sessions and keep the best representatives
- Bundle builder: curated upload set with coverage and risk summary
- Upload history: bundle status, receipt, and downstream processing state
- Policy center: redaction rules, excluded projects, blocked domains, contributor identity settings

Core actions:

- mark as irrelevant
- shortlist
- approve for upload
- redact string
- redact span
- exclude project
- split session into shareable spans

The session detail page should emphasize useful signal, not just transcript text:

- what task was attempted
- what context was available
- what tools were used
- whether the task converged
- what evidence exists for outcome quality

### Inbox design

The inbox should be a triage surface, not a transcript reader.

Each trace row/card should show:

- title inferred from task intent
- source and model
- repo / project
- duration and message count
- outcome badge such as `tests passed`, `build failed`, `analysis only`, `unknown`
- value badge such as `novel domain`, `long horizon`, `tool rich`, `scientific workflow`
- risk badge such as `names detected`, `private URL`, `manual review required`
- current status: `new`, `shortlisted`, `approved`, `blocked`

Primary inbox actions should be one-click:

- `Ignore`
- `Shortlist`
- `Open review`
- `Approve`

The user should be able to clear 50 sessions quickly without opening each one.

### Search design

Search is the center of the product, not a secondary filter box.

It should support four modes at launch:

- transcript text search
- metadata search by source, project, model, date, tags, outcome, status
- saved filters such as `Codex + biology + passed tests + not uploaded`
- semantic grouping later, not in v1

Recommended query model:

- simple search bar for free text
- visible facet chips below it
- advanced filters in a right-side drawer

Example saved searches:

- `OpenClaw sessions touching protein folding repos`
- `Codex traces with test execution and manual review pending`
- `Claude Code sessions from last 14 days with notebook or python tool use`

Under the hood, v1 should use SQLite FTS plus structured metadata filters. That is enough. A vector index can wait.

### Session detail design

The session view should be three-pane.

Left pane:

- timeline of messages and tool calls
- jump links to important events such as first prompt, long tool run, test execution, final answer

Center pane:

- transcript with collapsible assistant thinking blocks
- inline redaction markers
- span selection for exclude or redact

Right pane:

- metadata summary
- sensitivity findings
- files touched
- commands run
- outcome evidence
- reviewer notes

This layout matters because review is a synthesis task. The reviewer needs transcript, context, and policy state visible together.

### Review ergonomics

The reviewer should never need to manually inspect every token of every trace.

So the UI should surface "review shortcuts":

- auto-jump to suspected sensitive spans
- auto-jump to first and last user goals
- summarize files and commands touched
- collapse repetitive tool logs by default
- highlight high-value events such as failing tests, fix attempts, final verification

The UI should also support "reasoned approval":

- `Approved: good scientific debugging trace`
- `Approved: strong outcome verification`
- `Blocked: internal partner name`
- `Blocked: too much proprietary code context`

Those reason codes become useful later in ranking and partner reporting.

### Bundle builder

The bundle builder is where Ground Foundry's supply quality gets created.

It should answer three questions before upload:

1. Is this bundle safe?
2. Is this bundle diverse?
3. Is this bundle useful?

Recommended bundle summary panel:

- total sessions and spans
- source distribution
- project distribution
- scientific domain distribution
- risk summary
- reason-for-selection breakdown
- estimated redundancy score

Recommended bundle actions:

- remove risky traces
- deduplicate near-identical traces
- require manual review for unresolved risk flags
- attach submission note such as `computational biology debugging week 11`

### Upload flow

The upload flow should feel formal and explicit.

Recommended steps:

1. `Review`: show final bundle and unresolved risks
2. `Attest`: contributor affirms they have rights to share and reviewed sensitive data
3. `Upload`: send bundle to Ground Foundry
4. `Receipt`: show immutable receipt, bundle hash, and server status

This is important for trust. Scientists should always know exactly what left their machine and when.

### Multi-user and lab mode

Even if the first users are individuals, the UI should be designed so lab admins fit later.

That means planning for:

- multiple contributor profiles on one machine
- team policy packs
- shared deny lists
- review delegation
- lab-specific upload destinations

Do not build all of that now, but do not hard-code the product around a single anonymous user either.

### C. Policy and privacy layer

The current CLI already has strong review instincts: explicit source choice, project listing, secret redaction, exact-name scan, manual scan, and a gated push. Keep those principles and make them first-class in the workbench.

Additions needed for Ground Foundry:

- organization policy packs
- project-level sensitivity labels
- server-side deny lists synced to the client
- span redaction, not only string redaction
- reviewer notes and reason codes for every exclusion

Policy result for each session:

- `blocked`: cannot be uploaded
- `review_required`: scientist must inspect before upload
- `approved`: can be included in a bundle

Recommended policy UX:

- global rules for strings, usernames, domains, and projects
- per-trace findings with accept/block/redact actions
- "why was this flagged?" explanations for every detector hit
- a final pre-upload checklist that mirrors the current `dataclaw confirm` discipline

The existing CLI confirmation flow should survive as the backend contract even if the UI is friendlier on top.

### D. Upload gateway

Uploading to Ground Foundry should not reuse the Hugging Face publishing path directly.

Instead, upload a signed bundle to an authenticated Ground Foundry API.

Bundle contents:

- `manifest.json`
- `sessions.jsonl`
- `redactions.json`
- `provenance.json`
- optional local artifacts summary such as tests or notebook outputs

The server should separate:

- contributor identity
- dataset membership
- buyer-facing distribution packages

That separation keeps scientist trust high and allows downstream packaging without mutating the original reviewed bundle.

Recommended server-visible objects:

- `contributor`
- `workspace`
- `bundle`
- `trace`
- `policy_pack`
- `processing_job`

The client should only upload bundles. The server can unpack them into trace-level records afterward.

## Data model additions

The current session schema is a good base. The workbench needs extra fields for review and curation.

Suggested top-level additions:

- `source`: `claude | codex | openclaw | ...`
- `review_status`: `new | shortlisted | approved | blocked | uploaded`
- `sensitivity_score`
- `outcome_signals`
- `scientific_domain`
- `task_type`
- `selection_reason`
- `redaction_events`
- `consent_attestation_id`
- `bundle_id`

Suggested derived objects:

- `session_summary`: compact preview for inbox UI
- `trace_span`: shareable sub-range within a session
- `bundle`: immutable upload unit with provenance hash

UI-specific derived fields worth computing locally:

- `display_title`
- `risk_badges`
- `value_badges`
- `outcome_badges`
- `review_shortcuts`
- `duplicate_cluster_id`
- `upload_eligibility`

## System architecture

### Local machine

- source adapters read Claude/Codex/OpenClaw logs
- normalizer converts them into Ground Foundry trace schema
- local store keeps raw and reviewed forms
- review app reads from local store
- bundler creates immutable upload packages

Recommended local storage layout:

- SQLite for metadata, queues, filters, and FTS search
- filesystem blob store for raw session payloads
- derived summaries cached separately so list views remain fast

This avoids forcing large transcript payloads through every search query.

### Ground Foundry server

- ingestion API accepts bundles
- validation service verifies schema, signatures, and policy compliance
- refinery pipeline enriches traces with task tags, rubrics, and quality scores
- warehouse stores raw submitted bundle and derived datasets separately
- curator console manages downstream packaging and partner delivery

## Recommended implementation sequence for the UI

### v1

- local daemon in Python
- SQLite index with FTS
- localhost web UI
- inbox, session detail, bundle builder, upload receipt
- string redaction and project exclusion

### v1.5

- span-level redaction
- saved searches
- duplicate clustering
- richer outcome extraction

### v2

- desktop wrapper
- team workspaces
- lab policy packs
- reviewer assignment and audit trails

## Non-goals for the first release

These are tempting but should wait:

- vector search
- collaborative real-time review
- direct editing of raw imported transcripts
- automatic upload
- broad workflow capture outside coding/scientific agent traces

## Why this is the right MVP

It matches the memo's sequencing.

Near-term, the revenue engine is coding and scientific traces with clean verification. A scientist workbench built on top of `dataclaw` gets Ground Foundry to a usable collection layer quickly:

- existing parsers already cover the most important coding-agent sources
- staged review and confirmation logic already exists conceptually
- the missing work is mostly around continuous tracking, review UX, bundle semantics, and server upload

This keeps the first product narrow:

- individual researchers and small labs
- coding and computational science workflows
- local-first review and selective upload

That is enough to start generating authentic traces with defensible consent and provenance.

## Proposed rollout

### Phase 1: local curation MVP

- add local session index
- add review statuses and shortlist flow
- export curated bundle locally
- no server dependency yet

Success metric:

- a scientist can review one week of traces and produce a curated bundle in under 20 minutes

### Phase 2: Ground Foundry upload

- authenticated upload API
- signed bundle manifest
- server receipt + processing status
- project policy packs and synced deny lists

Success metric:

- approved local bundle can be uploaded and audited end-to-end with no manual ops intervention

### Phase 3: quality refinery

- automatic task classification
- outcome signal extraction
- deduplication and novelty ranking
- researcher-facing value score to guide selection

Success metric:

- top-ranked uploaded traces outperform random sampled traces on downstream evaluator preference

## Concrete next build steps

1. Extend `dataclaw` from one-shot export to persistent local indexing.
2. Add review metadata and bundle manifests to the schema.
3. Build a minimal local review UI on top of the index.
4. Replace direct Hugging Face publish as the primary path with a Ground Foundry upload target.
5. Preserve the existing gated review flow as the compliance backbone.

## Open decisions

These need product answers before implementation goes far:

- Is the first review surface a CLI/TUI, local web app, or desktop app?
- Do scientists upload full sessions, selected spans, or both?
- Should Ground Foundry know the contributor identity directly, or should the client mint pseudonymous contributor IDs?
- Which outcome signals matter most for science workflows beyond code execution: notebook runs, benchmark deltas, paper-writing progress, or experiment metadata?
- Is upload destination a single Ground Foundry tenant, or partner-specific buckets with separate policy packs?
