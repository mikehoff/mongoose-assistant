"""agent_output_hygiene.py: turns raw Claude Code CLI output (used by Mongoose's OWN
self-patch and overnight-build features) into an evidence-shaped status an owner can trust,
instead of a truncated dump of a subprocess's stdout/stderr.

Scope: this is about MONGOOSE's product surfaces (self_patch status, bug_workflow build
logs), which are genuinely status reports a normal owner reads to decide whether to approve
a staged change. It has nothing to do with, and cannot affect, how any chat interface
displays its own reasoning; that is a separate product surface this module does not touch.

The distinction that matters: exploratory, self-correcting reasoning during real debugging
is not itself a problem, it is how debugging legitimately works. The problem is presenting
that raw material AS a status report. This module keeps the evidence (file paths, command
names, test names, PASS/FAIL/ERROR, tracebacks) and drops exploratory narration
("maybe I edited the wrong block", "let me check that again") from the OWNER-FACING summary,
without ever hiding a real failure or error behind a cleaner-sounding line.
"""
from __future__ import annotations

import json
import re

# Lines that are evidence, kept verbatim: file paths, pass/fail/error markers, tracebacks,
# command-looking lines, and test names. Deliberately permissive: when in doubt, keep the
# line, since dropping a genuine error is far worse than keeping one exploratory line.
_EVIDENCE_RE = re.compile(
    r"(?:^Traceback|error|Error|ERROR|FAILED|failed|passed|PASSED|assert|Assert|"
    r"\.py:\d+|/[\w.-]+/[\w.-]+\.py|^\+|^-|test_\w+|\bok=\w+|\bstatus[=:]|\bexit code\b)"
)
# Exploratory/self-correcting narration: kept OUT of the owner-facing summary specifically
# because it reads as uncertainty about whether the work is actually correct, not because
# uncertainty during debugging is bad. A person approving a staged self-patch should see what
# happened, not the back-and-forth of figuring it out.
_NARRATION_RE = re.compile(
    r"\b(?:maybe|perhaps|I think|let me (?:check|try|see|look)|I'll (?:try|check)|"
    r"probably|not sure|wait,|actually,|hmm)\b", re.IGNORECASE)


def sanitize_agent_output(raw: str, max_chars: int = 3000) -> str:
    """Plain-text stdout/stderr -> an evidence-only tail, for self_patch's codegen result."""
    if not raw:
        return raw
    kept = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _EVIDENCE_RE.search(line):
            kept.append(line)
        elif not _NARRATION_RE.search(line) and len(stripped) < 200:
            # Short, non-narration lines (e.g. a plain status word) are ambiguous; keep them
            # rather than risk dropping something evidence-shaped the regex above missed.
            kept.append(line)
    out = "\n".join(kept) if kept else raw
    return out[-max_chars:]


def summarize_stream_json(raw: str, max_chars: int = 4000) -> str:
    """Claude Code CLI's --output-format stream-json output -> an evidence ledger: which
    tools ran, on which files, and the final result text, dropping intermediate reasoning
    content blocks. Falls back to sanitize_agent_output on anything that isn't valid
    stream-json (a format change or partial output must never crash the build step)."""
    if not raw:
        return raw
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    if not events:
        return sanitize_agent_output(raw, max_chars=max_chars)

    ledger: list[str] = []
    for ev in events:
        etype = ev.get("type", "")
        if etype == "assistant":
            content = ev.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = block.get("name", "tool")
                    inp = block.get("input", {}) or {}
                    target = inp.get("path") or inp.get("file_path") or inp.get("command", "")
                    target = str(target)[:120]
                    ledger.append(f"tool: {name} {target}".rstrip())
        elif etype == "result":
            result_text = str(ev.get("result", "") or "")[:500]
            if result_text:
                ledger.append(f"result: {result_text}")
            if "is_error" in ev:
                ledger.append(f"is_error: {ev['is_error']}")
    if not ledger:
        return sanitize_agent_output(raw, max_chars=max_chars)
    out = "\n".join(ledger)
    return out[-max_chars:]
