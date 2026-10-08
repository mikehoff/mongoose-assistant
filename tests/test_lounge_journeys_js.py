"""Runs the REAL shipped lounge JS (extracted verbatim from cockpit.py at test time, so it can
never drift from production) through eight user journeys in a real DOM: opening a quiet room,
the starter rendering, sending, receiving mongoose's reply exactly once, the concurrent-poll
race (which this harness caught as a live duplication bug on first run), hostile content
staying inert text, tab-leave stopping the timer without duplicating history on return, and a
send failure being visible rather than silent. Skips cleanly where node or jsdom is absent;
runs fully in CI and the build sandbox."""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest


def test_lounge_user_journeys_in_a_real_dom(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    jsdom = pathlib.Path("/home/claude/node_modules/jsdom")
    probe = subprocess.run([node, "-e", "require(process.env.JSDOM_PATH || 'jsdom')"],
                           env={"JSDOM_PATH": str(jsdom) if jsdom.exists() else "jsdom",
                                "PATH": "/usr/bin:/bin"},
                           capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("jsdom not available")

    src = pathlib.Path("src/mongoose/cockpit.py").read_text()
    start = src.index("// ── the lounge (IRC tab)")
    marker = "if(inp) inp.focus();\n}"
    end = src.index(marker, start) + len(marker)
    block = tmp_path / "irc_block.js"
    block.write_text(src[start:end])

    r = subprocess.run(
        [node, "tests/assets/lounge_journeys.js", str(block)],
        env={"JSDOM_PATH": str(jsdom) if jsdom.exists() else "jsdom", "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"journeys failed:\n{r.stdout}\n{r.stderr}"
    assert "ALL 9 LOUNGE JOURNEYS PASS" in r.stdout
