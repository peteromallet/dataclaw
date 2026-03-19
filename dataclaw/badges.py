"""Compute trace card badges for the scientist workbench inbox."""

import re

from .secrets import scan_text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_READ_ONLY_TOOLS = frozenset({
    "Read", "Glob", "Grep", "cat", "ls", "find", "head", "tail",
    "View", "Search", "ListFiles", "read_file", "search_files",
})

_SCIENTIFIC_LIBS = re.compile(
    r"\b(?:numpy|scipy|pandas|matplotlib|seaborn|plotly|biopython|BioPython"
    r"|rdkit|pytorch|torch|tensorflow|keras|jax|flax"
    r"|astropy|sympy|scikit-learn|sklearn|statsmodels"
    r"|openmm|mdtraj|pymatgen|ase|dask|xarray"
    r"|protein|genome|genomic|molecular|quantum|spectral"
    r"|phylogen|metabol|transcriptom|proteom)\b",
    re.IGNORECASE,
)

_SCIENTIFIC_EXTENSIONS = re.compile(
    r"\.(ipynb|csv|tsv|h5|hdf5|fasta|fastq|pdb|cif|mol2|sdf|npy|npz|parquet|feather)\b"
)

_SCIENTIFIC_TERMS = re.compile(
    r"\b(?:experiment|hypothesis|analysis|dataset|correlation|regression"
    r"|p-value|pvalue|chi-square|t-test|anova|standard.deviation"
    r"|confidence.interval|null.hypothesis|statistical|bayesian"
    r"|clustering|classification|training.data|test.data|validation)\b",
    re.IGNORECASE,
)

_PRIVATE_URL = re.compile(
    r"https?://(?:"
    r"[a-zA-Z0-9._-]+\.(?:local|internal|corp|intranet|lan)"
    r"|localhost(?::\d+)?"
    r"|(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d+\.\d+"
    r")\b",
)

# Heuristic: two or more capitalized words in sequence that look like a name.
# Excludes common code patterns (CamelCase identifiers without spaces).
_PROPER_NAME = re.compile(
    r"(?<![A-Za-z])"                     # not preceded by a letter
    r"[A-Z][a-z]{1,15}"                  # first name
    r"(?:\s+[A-Z][a-z]{1,15}){1,3}"     # last name (and optional middle)
    r"(?![A-Za-z])",                      # not followed by a letter
)

# Common false-positive name patterns to skip
_NAME_ALLOWLIST = re.compile(
    r"\b(?:United States|New York|San Francisco|Los Angeles|Open Source"
    r"|Visual Studio|Stack Overflow|Pull Request|Merge Request"
    r"|Hello World|Status Code|Type Error|Value Error|Key Error"
    r"|Content Type|Access Control|No Content|Not Found"
    r"|Read Only|File System|Data Frame|Data Set"
    r"|Machine Learning|Deep Learning|Neural Network"
    r"|Test Case|Test Suite)\b",
)

_TASK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("debugging", re.compile(
        r"\b(?:fix|bug|error|broken|crash|issue|traceback|exception|failing|segfault)\b",
        re.IGNORECASE,
    )),
    ("feature", re.compile(
        r"\b(?:add|implement|create|build|new feature|introduce|support for)\b",
        re.IGNORECASE,
    )),
    ("refactor", re.compile(
        r"\b(?:refactor|clean\s*up|reorganize|rename|move|restructure|simplify)\b",
        re.IGNORECASE,
    )),
    ("analysis", re.compile(
        r"\b(?:analyze|analyse|investigate|explore|understand|look at|inspect|audit)\b",
        re.IGNORECASE,
    )),
    ("testing", re.compile(
        r"\b(?:write tests?|add tests?|test coverage|spec|unit test|integration test)\b",
        re.IGNORECASE,
    )),
    ("documentation", re.compile(
        r"\b(?:document|readme|docstring|comment|changelog|update docs)\b",
        re.IGNORECASE,
    )),
    ("exploration", re.compile(
        r"\b(?:how does|what is|explain|show me|walk me through|help me understand)\b",
        re.IGNORECASE,
    )),
]


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def _iter_all_text(session: dict) -> list[str]:
    """Collect all textual content from a session into a flat list."""
    texts: list[str] = []
    for msg in session.get("messages", []):
        if msg.get("content"):
            texts.append(msg["content"])
        if msg.get("thinking"):
            texts.append(msg["thinking"])
        for tu in msg.get("tool_uses", []):
            inp = tu.get("input")
            if isinstance(inp, str):
                texts.append(inp)
            elif isinstance(inp, dict):
                for v in inp.values():
                    if isinstance(v, str):
                        texts.append(v)
            out = tu.get("output")
            if isinstance(out, str):
                texts.append(out)
            elif isinstance(out, dict):
                for v in out.values():
                    if isinstance(v, str):
                        texts.append(v)
    return texts


def _iter_tool_outputs(session: dict) -> list[str]:
    """Collect all tool-use output strings."""
    outputs: list[str] = []
    for msg in session.get("messages", []):
        for tu in msg.get("tool_uses", []):
            out = tu.get("output")
            if isinstance(out, str):
                outputs.append(out)
            elif isinstance(out, dict):
                for v in out.values():
                    if isinstance(v, str):
                        outputs.append(v)
    return outputs


def _get_all_tool_uses(session: dict) -> list[dict]:
    """Return a flat list of every tool_use dict in the session."""
    tool_uses: list[dict] = []
    for msg in session.get("messages", []):
        tool_uses.extend(msg.get("tool_uses", []))
    return tool_uses


def _get_user_messages(session: dict) -> list[str]:
    """Return content strings from user messages."""
    return [
        msg["content"]
        for msg in session.get("messages", [])
        if msg.get("role") == "user" and msg.get("content")
    ]


# ---------------------------------------------------------------------------
# Badge functions
# ---------------------------------------------------------------------------

def compute_outcome_badge(session: dict) -> str:
    """Determine the outcome of a session from tool outputs.

    Returns one of: tests_passed, tests_failed, build_failed, analysis_only, unknown
    """
    tool_uses = _get_all_tool_uses(session)

    if not tool_uses:
        return "analysis_only"

    # Check if all tools are read-only
    tool_names = {tu.get("tool", "") for tu in tool_uses}
    if tool_names and tool_names <= _READ_ONLY_TOOLS:
        return "analysis_only"

    outputs = _iter_tool_outputs(session)
    combined = "\n".join(outputs)

    # Check for test results -- scan in priority order (failures trump passes)
    has_test_pass = False
    has_test_fail = False
    has_build_fail = False

    for output in outputs:
        # Build failures (check first so "BUILD FAILED" isn't caught as test failure)
        if re.search(r"BUILD FAILED|build failed", output):
            has_build_fail = True
        if re.search(r"(?:compile|compilation)\s+(?:error|failed)", output, re.IGNORECASE):
            has_build_fail = True
        if re.search(r"error\[E\d+\]", output):  # Rust compiler errors
            has_build_fail = True
        if re.search(r"error TS\d+:", output):  # TypeScript errors
            has_build_fail = True

        # Test failures (exclude lines that are build failures)
        if re.search(r"(?<!BUILD\s)FAILED\s+\S+::", output):
            has_test_fail = True
        if re.search(r"\d+\s+failed", output):
            has_test_fail = True
        if re.search(r"AssertionError|FAIL:|Tests?:\s*\d+\s+failed", output):
            has_test_fail = True
        if re.search(r"FAILURES|failures=\d*[1-9]", output):
            has_test_fail = True

        # Test passes
        if re.search(r"\d+\s+passed", output):
            has_test_pass = True
        if re.search(r"\bpassed\b", output) and re.search(r"pytest|jest|mocha|vitest", output, re.IGNORECASE):
            has_test_pass = True
        if re.search(r"\bOK\b", output) and re.search(r"tests?\s+run|Ran\s+\d+", output, re.IGNORECASE):
            has_test_pass = True
        if re.search(r"Tests?:\s+\d+\s+passed,\s+\d+\s+total", output):
            has_test_pass = True
        if re.search(r"✓|All tests passed|BUILD SUCCESSFUL", output):
            has_test_pass = True

    # Priority: test failures > build failures > test passes
    if has_test_fail:
        return "tests_failed"
    if has_build_fail:
        return "build_failed"
    if has_test_pass:
        return "tests_passed"

    return "unknown"


def compute_value_badges(session: dict) -> list[str]:
    """Compute value signal badges.

    Possible badges: novel_domain, long_horizon, tool_rich, scientific_workflow, debugging
    """
    badges: list[str] = []
    stats = session.get("stats", {})
    all_text = "\n".join(_iter_all_text(session))

    # novel_domain: specialized/scientific libraries
    if _SCIENTIFIC_LIBS.search(all_text):
        badges.append("novel_domain")

    # long_horizon
    user_msgs = stats.get("user_messages", 0)
    total_tokens = stats.get("input_tokens", 0) + stats.get("output_tokens", 0)
    if user_msgs > 20 or total_tokens > 50_000:
        badges.append("long_horizon")

    # tool_rich
    total_msgs = stats.get("user_messages", 0) + stats.get("assistant_messages", 0)
    tool_count = stats.get("tool_uses", 0)
    if tool_count / max(total_msgs, 1) > 0.5:
        badges.append("tool_rich")

    # scientific_workflow
    if _SCIENTIFIC_EXTENSIONS.search(all_text) or _SCIENTIFIC_TERMS.search(all_text):
        badges.append("scientific_workflow")

    # debugging: error -> fix -> verify pattern
    messages = session.get("messages", [])
    if len(messages) >= 3:
        # Split messages into thirds
        third = max(len(messages) // 3, 1)
        early = "\n".join(
            msg.get("content", "") for msg in messages[:third] if msg.get("content")
        )
        late = "\n".join(
            msg.get("content", "") for msg in messages[third * 2:] if msg.get("content")
        )
        late_tools = "\n".join(
            tu.get("output", "")
            for msg in messages[third * 2:]
            for tu in msg.get("tool_uses", [])
            if isinstance(tu.get("output"), str)
        )

        has_early_error = bool(re.search(
            r"\b(?:error|bug|broken|crash|traceback|exception|failing)\b",
            early, re.IGNORECASE,
        ))
        has_late_verify = bool(re.search(
            r"\b(?:passed|works|fixed|resolved|success|OK|verified)\b",
            late + " " + late_tools, re.IGNORECASE,
        ))
        if has_early_error and has_late_verify:
            badges.append("debugging")

    return badges


def compute_risk_badges(session: dict) -> list[str]:
    """Compute privacy/sensitivity risk badges.

    Possible badges: secrets_detected, names_detected, private_url, manual_review
    """
    badges: list[str] = []
    all_texts = _iter_all_text(session)
    combined = "\n".join(all_texts)

    # secrets_detected
    secrets_found = False
    for text in all_texts:
        if scan_text(text):
            secrets_found = True
            break
    if secrets_found:
        badges.append("secrets_detected")

    # names_detected
    names_found = False
    for text in all_texts:
        for m in _PROPER_NAME.finditer(text):
            name = m.group(0)
            if not _NAME_ALLOWLIST.search(name):
                names_found = True
                break
        if names_found:
            break
    if names_found:
        badges.append("names_detected")

    # private_url
    if _PRIVATE_URL.search(combined):
        badges.append("private_url")

    # manual_review: based on sensitivity score
    score = compute_sensitivity_score(session)
    if score >= 0.5:
        badges.append("manual_review")

    return badges


def compute_sensitivity_score(session: dict) -> float:
    """Compute a 0.0-1.0 sensitivity score based on findings count and types.

    Higher score = more review needed.
    Weights: secrets (0.3 each, cap at 1.0), names (0.1 each), private_urls (0.15 each)
    """
    all_texts = _iter_all_text(session)
    combined = "\n".join(all_texts)

    secret_count = 0
    name_count = 0
    url_count = 0

    for text in all_texts:
        secret_count += len(scan_text(text))

    for m in _PROPER_NAME.finditer(combined):
        if not _NAME_ALLOWLIST.search(m.group(0)):
            name_count += 1

    url_count = len(_PRIVATE_URL.findall(combined))

    score = (
        min(secret_count * 0.3, 1.0)
        + min(name_count * 0.1, 1.0)
        + min(url_count * 0.15, 1.0)
    )
    # Clamp to [0.0, 1.0]
    return min(score, 1.0)


def compute_task_type(session: dict) -> str:
    """Infer the task type from conversation content.

    Returns one of: debugging, feature, refactor, analysis, testing, documentation, exploration, unknown
    """
    user_msgs = _get_user_messages(session)
    # Check the first few user messages for intent signals
    text = "\n".join(user_msgs[:5])

    if not text:
        return "unknown"

    # Score each task type by number of keyword matches
    best_type = "unknown"
    best_score = 0

    for task_type, pattern in _TASK_PATTERNS:
        matches = pattern.findall(text)
        if len(matches) > best_score:
            best_score = len(matches)
            best_type = task_type

    return best_type


_INTERNAL_TAG_RE = re.compile(
    r"^\s*<(command-message|local-command-caveat|command-name|local-command-stdout)\b[^>]*>"
    r".*?</\1>\s*$",
    re.DOTALL,
)
_SKIP_PATTERNS = [
    _INTERNAL_TAG_RE,
    re.compile(r"^\s*\[Request interrupted by user\]\s*$"),
    # Single-word terse commands (init, install, exit, help, etc.)
    re.compile(r"^\s*[a-z]{2,12}\s*$"),
]
_XML_TAG_RE = re.compile(r"<[^>]+>")


def _is_skippable_message(text: str) -> bool:
    """Return True if *text* is an internal command or too terse to be a title."""
    return any(p.match(text) for p in _SKIP_PATTERNS)


def compute_display_title(session: dict) -> str:
    """Extract a short display title from the first real user message.

    Skips internal Claude Code command messages (XML-wrapped slash commands,
    local-command wrappers, etc.) and strips XML/HTML tags from the result.
    Truncates to ~80 chars.
    """
    fallback = session.get("project", "Untitled session")
    source = session.get("source", "")
    if source and fallback == "Untitled session":
        fallback = f"{source}:{session.get('project', 'unknown')}"

    user_msgs = _get_user_messages(session)
    if not user_msgs:
        return fallback

    # Find the first real user message (skip internal commands)
    text = ""
    for msg in user_msgs:
        if not _is_skippable_message(msg):
            text = msg.strip()
            break

    if not text:
        return fallback

    # Strip any remaining XML/HTML tags
    text = _XML_TAG_RE.sub("", text).strip()

    if not text:
        return fallback

    # Strip common conversational prefixes
    prefixes = [
        "Can you ", "Could you ", "Please ", "I need you to ", "I want you to ",
        "I'd like you to ", "Help me ", "Let's ", "Let me ",
    ]
    for prefix in prefixes:
        if text.startswith(prefix):
            text = text[len(prefix):]
            # Capitalize the first letter after stripping
            if text:
                text = text[0].upper() + text[1:]
            break

    # Take the first line only
    first_line = text.split("\n", 1)[0].strip()

    # Truncate
    if len(first_line) > 80:
        # Try to break at a word boundary
        truncated = first_line[:77]
        last_space = truncated.rfind(" ")
        if last_space > 40:
            truncated = truncated[:last_space]
        first_line = truncated + "..."

    return first_line if first_line else fallback


def compute_all_badges(session: dict) -> dict:
    """Compute all badges and signals for a session.

    Returns dict with keys:
    - display_title: str
    - outcome_badge: str
    - value_badges: list[str]
    - risk_badges: list[str]
    - sensitivity_score: float
    - task_type: str
    - files_touched: list[str]
    - commands_run: list[str]
    """
    # Extract files touched and commands run from tool uses
    files_touched: list[str] = []
    commands_run: list[str] = []
    seen_files: set[str] = set()
    seen_commands: set[str] = set()

    for msg in session.get("messages", []):
        for tu in msg.get("tool_uses", []):
            tool = tu.get("tool", "")
            inp = tu.get("input")

            # Extract file paths
            if isinstance(inp, dict):
                for key in ("file_path", "path", "file", "filename"):
                    val = inp.get(key)
                    if isinstance(val, str) and val not in seen_files:
                        seen_files.add(val)
                        files_touched.append(val)

                # Extract commands
                if tool in ("Bash", "bash", "execute_command", "run_command"):
                    cmd = inp.get("command", "")
                    if isinstance(cmd, str) and cmd and cmd not in seen_commands:
                        seen_commands.add(cmd)
                        commands_run.append(cmd)
            elif isinstance(inp, str):
                # Some tools pass input as a plain string (e.g. command)
                if tool in ("Bash", "bash") and inp not in seen_commands:
                    seen_commands.add(inp)
                    commands_run.append(inp)

    return {
        "display_title": compute_display_title(session),
        "outcome_badge": compute_outcome_badge(session),
        "value_badges": compute_value_badges(session),
        "risk_badges": compute_risk_badges(session),
        "sensitivity_score": compute_sensitivity_score(session),
        "task_type": compute_task_type(session),
        "files_touched": files_touched,
        "commands_run": commands_run,
    }
