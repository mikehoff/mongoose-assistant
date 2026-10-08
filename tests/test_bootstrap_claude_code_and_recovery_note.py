"""Two field bugs in bootstrap_macos.sh, locked down:
1. Claude Code install failures were completely silent (output redirected to /dev/null), hiding
   the near-universal macOS cause (a non-writable Homebrew npm global directory, EACCES). The
   script must detect that, switch to a user-owned prefix without sudo, and on any remaining
   failure print the REAL npm output instead of a generic message.
2. A stuck "Mongoose is offline" page caused by a pre-1.21.3 stale service worker has exactly
   one manual recovery step; it must be printed plainly (bold red -- true blink is unreliable
   in modern terminals) on every https bootstrap run, not left to be rediscovered by trial and
   error, and must not overclaim automation Apple does not permit from a script."""
from __future__ import annotations

import pathlib
import warnings

warnings.filterwarnings("ignore")

ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOTSTRAP = (ROOT / "scripts" / "bootstrap_macos.sh").read_text()


def _claude_code_segment() -> str:
    start = BOOTSTRAP.index("Checking Claude Code CLI")
    end = BOOTSTRAP.index("Checking Ollama")
    return BOOTSTRAP[start:end]


def test_npm_install_output_is_no_longer_suppressed():
    seg = _claude_code_segment()
    # the actual install call must capture output; existence checks (command -v) may still
    # legitimately discard theirs
    assert "npm install -g @anthropic-ai/claude-code >/dev/null 2>&1" not in seg
    assert "NPM_OUT=" in seg
    assert "tail -n 10" in seg


def test_detects_and_fixes_unwritable_npm_prefix_without_sudo():
    seg = _claude_code_segment()
    assert 'if [[ -n "$NPM_PREFIX" ]] && ! [[ -w "$NPM_PREFIX" ]]' in seg
    assert "npm config set prefix" in seg
    assert ".npm-global" in seg
    # no command in this segment ever INVOKES sudo (prose explaining that it is avoided is fine)
    assert not any(line.strip().startswith("sudo ") for line in seg.splitlines())


def test_pathfix_is_persisted_for_future_terminals():
    seg = _claude_code_segment()
    assert ".zshrc" in seg
    assert "npm-global/bin" in seg


def test_red_recovery_note_present_and_honest_about_limits():
    start = BOOTSTRAP.index('mongoose_probe_scheme)" == "https"')
    seg = BOOTSTRAP[start:start + 3000]
    assert "RED_BOLD=" in seg
    assert "Manage Website Data" in seg
    assert 'search \\"localhost\\"' in seg or 'search "localhost"' in seg
    # honesty: no LIVE blink escape sequence is ever emitted by an echo/printf in this segment
    # (a comment explaining why blink is avoided is fine and expected to mention it)
    for line in seg.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if ("printf" in stripped or "echo" in stripped) and "[5m" in stripped:
            raise AssertionError(f"live blink escape emitted: {stripped!r}")
