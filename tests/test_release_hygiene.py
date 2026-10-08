"""Release-hygiene guards from the external review:
  - launcher/bootstrap scripts must not carry stale literal version numbers (they read release.json),
  - all markdown docs must have balanced code fences,
  - user-facing copy must be free of the ' ,' / ' .' punctuation artefacts,
  - requirements.txt must include the test-backstop deps it claims to need.
These are cheap and catch exactly the "looks duct-taped" first-impression regressions.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def test_launcher_scripts_have_no_stale_version_literals():
    """The launcher/bootstrap scripts must derive the version from release.json, not hardcode an old one."""
    scripts = ["Install Mongoose.command", "Stop Mongoose.command", "Mongoose Status.command",
               "scripts/bootstrap_macos.sh"]
    stale = re.compile(r"\b0\.5\d(\.\d+)?\b|\b1\.0\.0-alpha\b")
    offenders = []
    for s in scripts:
        txt = _read(ROOT / s)
        for ln in txt.splitlines():
            # ignore comment lines that merely discuss versioning
            if ln.strip().startswith("#"):
                continue
            if stale.search(ln):
                offenders.append((s, ln.strip()[:80]))
    assert not offenders, f"stale version literals in launcher scripts: {offenders}"


def test_all_markdown_has_balanced_code_fences():
    mds = [p for p in ROOT.rglob("*.md")
           if ".git" not in p.parts and "node_modules" not in p.parts]
    unbalanced = []
    for p in mds:
        # count triple-backtick FENCE markers only, not inline `code` spans
        fences = len(re.findall(r"^```", _read(p), re.MULTILINE))
        if fences % 2 != 0:
            unbalanced.append((str(p.relative_to(ROOT)), fences))
    assert not unbalanced, f"unbalanced code fences: {unbalanced}"


def test_no_space_before_punctuation_in_user_copy():
    """No ' ,' / ' .' artefacts in user-facing source files. (We check comma and period, the unambiguous
    prose artefacts; ' ?' and ' !' are skipped here because JS ternaries 'a ? b : c' legitimately put a
    space before '?', and disentangling those from prose isn't worth the false-positive risk.)"""
    files = ["src/mongoose/wizard.py", "src/mongoose/cockpit.py", "src/mongoose/setup_wizard.py",
             "src/mongoose/services/delight.py", "src/mongoose/services/music_taste.py",
             "src/mongoose/persona.py"]
    # space before a comma or period, where the next char ends the token (space/quote/newline/paren)
    artefact = re.compile(r" [,.](?=[ \"'\n)])")
    offenders = []
    for f in files:
        txt = _read(ROOT / f)
        for i, ln in enumerate(txt.splitlines(), 1):
            if artefact.search(ln):
                offenders.append((f, i, ln.strip()[:70]))
    assert not offenders, f"punctuation artefacts in user copy: {offenders[:10]}"


def test_requirements_includes_test_backstop_deps():
    """requirements.txt must include pytest-timeout (and pyflakes) so the README install path gets the
    hang backstop the QA scripts require , matching the 'test' extra."""
    req = _read(ROOT / "requirements.txt")
    assert "pytest-timeout" in req, "pytest-timeout missing from requirements.txt"
    assert "pyflakes" in req, "pyflakes missing from requirements.txt"


def test_no_space_comma_artefacts_in_any_string_constant():
    """A historical bad em-dash sweep left ' , ' (space-comma-space) artefacts in ~440 string
    literals across 78 files, and guests read them ('Talk something through , A calm space').
    The old per-file prose check missed them because it only covered six files. This walks the
    AST of EVERY src module and asserts no string constant carries the artefact, docstrings
    included (they surface in help output)."""
    import ast as _ast
    import pathlib as _pl
    offenders = []
    for p in _pl.Path("src/mongoose").rglob("*.py"):
        if "pycache" in str(p):
            continue
        for node in _ast.walk(_ast.parse(p.read_text())):
            if (isinstance(node, _ast.Constant) and isinstance(node.value, str)
                    and " , " in node.value):
                offenders.append((str(p), node.lineno))
    assert not offenders, f"space-comma artefacts in string constants: {offenders[:10]}"


def test_update_handover_kills_stray_watchdogs_from_any_install_folder():
    """Live incident on the real machine: /install spun forever while the terminal installer
    sat at 'configuring'. Cause: a stray server-watchdog process spawned from a DIFFERENT
    install folder (Downloads/mongoose 10 vs 12) reads its own relative data/ dir, so the
    teardown's pause file (written into THIS install's data dir) never reaches it, and
    launchctl bootout is a no-op for a plain process. The zombie resurrects the OLD server
    the moment the port is freed and the new server fights a respawn loop it cannot win.
    Teardown must therefore kill every server-watchdog PROCESS machine-wide (safe: the new
    serve spawns its own), and the failure diagnosis must name the watchdog check."""
    from pathlib import Path
    text = Path("scripts/update.sh").read_text()
    assert 'pkill -f "mongoose.cli server-watchdog"' in text
    assert "watchdog processes" in text  # failure diagnosis section


def test_serve_caps_graceful_shutdown_so_handover_cannot_deadlock():
    """Second layer of the same incident: uvicorn's default graceful shutdown waits FOREVER
    for in-flight connections, so a browser tab holding one (the /install page polling)
    turns handover into a three-way deadlock: terminal waits on server, server waits on
    browser, browser waits on server."""
    from pathlib import Path
    text = Path("src/mongoose/cli.py").read_text()
    assert "timeout_graceful_shutdown=5" in text


def test_service_worker_never_touches_the_install_flow():
    """Reconciliation of a parallel diagnosis (Dispatch): a STALE worker in the browser can
    swallow /install, and Clear-Site-Data provably cannot reach a worker that is already
    intercepting. The current worker already skips /install NAVIGATIONS (bare return, so
    cert interstitials surface natively); this extends the exemption to the whole install
    flow including the /install/progress poll, so no SW layer, current or future, adds
    timeout or cache semantics to an install in progress."""
    import warnings
    warnings.filterwarnings("ignore")
    from fastapi.testclient import TestClient
    import mongoose.app as A
    js = TestClient(A.app).get("/sw.js").text
    assert "url.pathname === '/install'" in js
    assert "startsWith('/install/')" in js
