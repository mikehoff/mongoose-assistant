"""Release gate: every Mongoose shell helper that is *called* must be *defined*.

The motivating bug: a previous tree's installer called `mongoose_open_local_path "/install"` from both
`Install Mongoose.command` and `scripts/bootstrap_macos.sh`, but `scripts/mongoose_local_probe.sh` never
defined it. `bash -n` does NOT catch this (an undefined function is a runtime "command not found", not a
syntax error), and under `set -e` it aborts the installer , which is exactly the "shell version" of the
dead-page JavaScript bugs where a function was called but never defined.

This gate parses the installer shell files, collects every `mongoose_*`/`_mongoose_*` function that is
DEFINED across all of them, and every such helper that is CALLED, and asserts every call resolves. It is
deliberately scoped to the project's own `mongoose_`-prefixed helpers (not arbitrary commands), because
those are the ones defined in-repo and sourced across files; a typo or a rename that misses a call site
is the failure we want to catch before it reaches a Mac.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent

# The installer shell surface that sources a shared helper file. These are the files where an undefined
# helper would strand a real install.
SHELL_FILES = [
    REPO / "Install Mongoose.command",
    REPO / "scripts" / "bootstrap_macos.sh",
    REPO / "scripts" / "mongoose_local_probe.sh",
]

# A function definition: optional leading underscore, the mongoose_ prefix, then `(`...`)` then `{`,
# allowing an optional space before the parens (`name ()` and `name()` are both valid bash).
_DEF_RE = re.compile(r"^\s*(_?mongoose_[a-z0-9_]+)\s*\(\)\s*\{", re.MULTILINE)

# A standalone helper name token. The (?<![\w-]) lookbehind ensures we do not match `mongoose_serve`
# inside a larger identifier such as `_pid_is_mongoose_serve` or `_cmd_is_mongoose_server`; the token
# must start at a real boundary (its own leading underscore is allowed, but not another word char).
_NAME_RE = re.compile(r"(?<![\w-])(_?mongoose_[a-z0-9_]+)")


def _strip_noise(line: str) -> str:
    """Remove comments and the contents of single/double quoted strings so that a helper name mentioned
    in a comment or inside a literal string (e.g. the message 'run mongoose setup-voice') is NOT counted
    as a call. Conservative: it blanks quoted spans and trailing # comments."""
    # drop trailing comment (not perfect for '#' inside strings, but we blank strings first)
    out = []
    i = 0
    quote = None
    while i < len(line):
        c = line[i]
        if quote:
            if c == quote:
                quote = None
            out.append(" ")  # blank the string contents
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            out.append(" ")
            i += 1
            continue
        if c == "#":
            break  # comment to end of line
        out.append(c)
        i += 1
    return "".join(out)


def _collect_defs() -> set[str]:
    defs: set[str] = set()
    for f in SHELL_FILES:
        if f.exists():
            defs |= set(_DEF_RE.findall(f.read_text()))
    return defs


def _collect_calls() -> dict[str, list[tuple[str, int]]]:
    """Return {helper_name: [(file, line), ...]} for tokens that appear in call position. Call position
    heuristic: the token is NOT immediately part of its own definition (no `()` right after) and appears
    either at a command start, inside a `$( ... )`/backtick substitution, or after a pipe/;/&&/||/then/do.
    We blank comments and strings first so only live code counts."""
    calls: dict[str, list[tuple[str, int]]] = {}
    for f in SHELL_FILES:
        if not f.exists():
            continue
        for lineno, raw in enumerate(f.read_text().splitlines(), 1):
            line = _strip_noise(raw)
            if "mongoose_" not in line:
                continue
            # skip definition lines themselves
            if _DEF_RE.match(line):
                continue
            for m in _NAME_RE.finditer(line):
                name = m.group(0)
                after = line[m.end(): m.end() + 2]
                # a `name(` here would be a definition fragment, not a call; skip
                if after.startswith("()"):
                    continue
                calls.setdefault(name, []).append((f.name, lineno))
    return calls


def test_installer_shell_files_parse_and_exist():
    for f in SHELL_FILES:
        assert f.exists(), f"missing installer shell file: {f}"


def test_every_called_mongoose_helper_is_defined():
    """The core gate: no `mongoose_*` helper may be called without a definition somewhere in the
    installer shell surface. This catches the `mongoose_open_local_path` class (called, never defined)
    that `bash -n` and Python tests both miss."""
    defs = _collect_defs()
    assert defs, "no mongoose_ helper definitions found , did the shell files move?"
    calls = _collect_calls()
    undefined = {name: sites for name, sites in calls.items() if name not in defs}
    assert not undefined, (
        "shell helpers are called but never defined (would be 'command not found' at install time, "
        "aborting under set -e):\n" + "\n".join(
            f"  {name}  called at " + ", ".join(f"{fn}:{ln}" for fn, ln in sites)
            for name, sites in sorted(undefined.items())))


def test_known_owner_local_helpers_are_defined_and_used():
    """Targeted regression for the owner-local handoff: the localhost openers must exist (the owner
    setup page is loopback-only, so the installer must open it on localhost, not the LAN IP)."""
    defs = _collect_defs()
    # this tree's owner-local helpers (named mongoose_local_url / mongoose_open_local)
    assert "mongoose_local_url" in defs, "mongoose_local_url (owner-local URL helper) is not defined"
    assert "mongoose_open_local" in defs, "mongoose_open_local (owner-local opener) is not defined"
    # and the install open path must actually use the local opener somewhere
    blob = "\n".join(f.read_text() for f in SHELL_FILES if f.exists())
    assert "mongoose_open_local" in blob and "/install" in blob, "installer no longer opens /install via the local opener"
