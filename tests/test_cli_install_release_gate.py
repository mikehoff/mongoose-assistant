"""Release gates for CLI/install failures that ordinary unit tests miss.

These tests deliberately exercise the same surfaces a real macOS installer uses:
* the monolithic ``main()`` dispatcher, where function-local imports can poison
  unrelated command branches via Python's local-variable scoping rules (the
  server-watchdog `os` bug);
* subprocess-style ``python -m mongoose.cli`` entrypoints, because direct service
  tests do not prove the console path works or that return codes propagate;
* the shell installer's phase truth table, so post-ready failures are not reported
  as "failed before ready".

Checks 1-3 are implementation-agnostic. Checks 4-5 assert THIS tree's bootstrap/wrapper, which solve
the auditor's Class C/D the same way the spec requires (capture+surface watchdog stderr; phase-aware
wrapper message) using this codebase's own variable names.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "src" / "mongoose" / "cli.py"
BOOTSTRAP = ROOT / "scripts" / "bootstrap_macos.sh"
WRAPPER = ROOT / "Install Mongoose.command"

# Names that are especially dangerous if imported unaliased inside main(): any one import makes the name
# local for the whole multi-thousand-line dispatcher, so a separate branch can hit UnboundLocalError
# even though the import is in an unrelated branch. (This is exactly the server-watchdog `os` bug.)
BANNED_UNALIASED_MAIN_IMPORTS = {
    "asyncio", "json", "os", "pathlib", "shutil", "socket", "ssl",
    "subprocess", "sys", "time", "urllib",
}


def _main_node() -> ast.FunctionDef:
    tree = ast.parse(CLI.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("mongoose.cli must define main()")


# --- Check 1: no unaliased stdlib imports in main() that can poison other branches ------------------
def test_main_dispatcher_has_no_unaliased_stdlib_imports_that_poison_other_branches():
    main = _main_node()
    offenders: list[str] = []
    for node in ast.walk(main):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in BANNED_UNALIASED_MAIN_IMPORTS and alias.asname is None:
                    offenders.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".", 1)[0]
            if root in BANNED_UNALIASED_MAIN_IMPORTS:
                for alias in node.names:
                    bound = alias.asname or alias.name
                    # an aliased private name (_os, _P, _sys) is safe; a bare bound name is not
                    if alias.asname is None or not bound.startswith("_"):
                        offenders.append(f"line {node.lineno}: from {node.module} import {alias.name}")
    assert offenders == [], (
        "Function-local imports in main() can make names local across unrelated command branches and "
        "cause UnboundLocalError. Move these to module scope, alias them as private names such as `_os`, "
        f"or split the subcommand into its own function: {offenders}")


# --- Check 2: module entrypoint propagates main() return codes (Class B) ----------------------------
def test_python_module_entrypoint_propagates_main_return_codes():
    src = CLI.read_text().rstrip()
    assert src.endswith("raise SystemExit(main())"), (
        "`python -m mongoose.cli ...` must propagate main() return codes. The setuptools console script "
        "does this automatically, but module execution must match it so local/manual/CI QA sees failures.")


# --- Check 3: real subprocess safety of the installer-only watchdog commands ------------------------
def _run_cli(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["HOME"] = str(tmp_path)
    env["MONGOOSE_DATA_DIR"] = str(tmp_path / "data")
    return subprocess.run(
        [sys.executable, "-m", "mongoose.cli", *args],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=20)


def test_server_watchdog_install_and_uninstall_are_real_subprocess_safe(tmp_path):
    install = _run_cli(["server-watchdog", "--install"], tmp_path)
    assert install.returncode == 0, install.stdout + install.stderr
    assert "Installed the server watchdog launch agent:" in install.stdout
    combined = install.stdout + install.stderr
    assert "local variable" not in combined
    assert "UnboundLocalError" not in combined
    assert "Traceback" not in combined

    uninstall = _run_cli(["server-watchdog", "--uninstall"], tmp_path)
    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    combined_u = uninstall.stdout + uninstall.stderr
    assert "local variable" not in combined_u
    assert "UnboundLocalError" not in combined_u
    assert "Traceback" not in combined_u


def test_server_watchdog_once_is_subprocess_safe(tmp_path):
    # --once runs a single probe tick; it must not crash on the scoping bug class either.
    r = _run_cli(["server-watchdog", "--once"], tmp_path)
    combined = r.stdout + r.stderr
    assert "UnboundLocalError" not in combined and "local variable" not in combined and "Traceback" not in combined


# --- Check 4: bootstrap watchdog is non-fatal after smoke AND surfaces its real stderr (Class D) -----
def test_bootstrap_watchdog_is_nonfatal_after_smoke_and_preserves_error_detail():
    boot = BOOTSTRAP.read_text()
    # isolate the watchdog section between its heading and the ready marker
    section = boot.split("Installing the server watchdog", 1)[1].split("_install-step ready done", 1)[0]
    # the install command captures stdout/stderr to files and does NOT abort under set -e
    assert 'server-watchdog --install >"$WD_OUT" 2>"$WD_ERR" || WD_RC=$?' in section
    # the real failure reason is surfaced (status + the captured stderr tail), not hidden
    assert "Watchdog install exited with status $WD_RC" in section
    assert "$WD_ERR" in section and "tail -20" in section
    # crucially: no fatal exit between the watchdog heading and marking the install ready
    assert "exit 1" not in section


# --- Check 5: wrapper message is phase-aware (Class C) ----------------------------------------------
def test_wrapper_truth_message_is_phase_aware():
    wrapper = WRAPPER.read_text()
    assert "if bash scripts/bootstrap_macos.sh" in wrapper
    # the wrapper decides from the install-phase state machine, not the raw exit code
    assert "_install-phase" in wrapper and "ready_proven" in wrapper
    # after readiness, the phase-true message is present and the fatal one is NOT used on that branch
    assert "Mongoose became ready, but a final post-install step failed." in wrapper
    assert "Install failed before Mongoose became ready." in wrapper
    # the fatal message must come AFTER the readiness check, i.e. only on the not-ready branch
    lines = wrapper.splitlines()
    ready_idx = next(i for i, l in enumerate(lines) if "READY_PROVEN" in l and "_install-phase" in l)
    fatal_idx = next(i for i, l in enumerate(lines)
                     if "printf" in l and "Install failed before Mongoose became ready" in l)
    postready_idx = next(i for i, l in enumerate(lines)
                         if "printf" in l and "Mongoose became ready, but a final post-install step failed." in l)
    assert ready_idx < postready_idx < fatal_idx


# --- banned-message contract (the spec's explicit acceptance test) ----------------------------------
def test_no_false_pre_ready_failure_message_path_when_ready_proven():
    """The wrapper must not be able to print 'failed before ready' on a branch where readiness was
    proven. We assert the fatal print sits strictly inside the not-ready (else) branch."""
    wrapper = WRAPPER.read_text()
    # everything from the ready_proven test to its matching fail branch
    after_check = wrapper.split("READY_PROVEN", 1)[1]
    proven_branch = after_check.split("else", 1)[0]
    assert "Install failed before Mongoose became ready." not in proven_branch, (
        "the fatal readiness-failure message is reachable on the ready_proven branch")


def test_wrapper_queries_phase_via_venv_not_bare_mongoose():
    """The shipped 1.1.7 bug: the wrapper queried `mongoose _install-phase`, but the console script is
    NOT on PATH at that point (it lives in .venv/bin; the PATH setup only affects future shells). The
    bare call silently failed, `|| echo false` set READY_PROVEN=false, and a fully-successful install
    was reported as 'failed before ready'. The wrapper must resolve the CLI through the venv."""
    wrapper = WRAPPER.read_text()
    assert ".venv/bin/mongoose" in wrapper, "wrapper must call the phase query via .venv/bin/mongoose"
    assert "MONGOOSE_CLI=" in wrapper, "wrapper should resolve a MONGOOSE_CLI that exists post-install"
    phase_lines = [l for l in wrapper.splitlines()
                   if "_install-phase" in l and "printf" not in l and not l.strip().startswith("#")]
    assert phase_lines, "wrapper no longer queries _install-phase"
    for l in phase_lines:
        assert "${MONGOOSE_CLI[@]}" in l or ".venv/bin/mongoose" in l, (
            f"phase query relies on a bare `mongoose` not guaranteed on PATH: {l.strip()}")


def test_wrapper_treats_clean_bootstrap_exit_as_ready_safety_net():
    """Defense in depth: even if the phase query can't run, a clean bootstrap exit (rc 0) means the
    install reached its ready marker, so the wrapper must treat it as ready and never print the fatal
    message. A successful install can never be misreported, whatever happens to the phase query."""
    wrapper = WRAPPER.read_text()
    assert "-eq 0" in wrapper and "bootstrap_rc" in wrapper, (
        "wrapper lacks the rc==0 safety net that forces READY_PROVEN=true on a clean bootstrap exit")


def test_install_done_screen_reveals_links_even_if_pairing_fails():
    """The dead-screen bug: showDone() threw on its first line (an unguarded getElementById().classList)
    and the success card with the cockpit link never appeared, leaving a bare 100% bar. showDone must
    reveal the done card before any fragile pairing/QR work, and guard that work so a pairing failure
    can't blank the already-shown success screen."""
    from mongoose.cockpit import install_html
    h = install_html()
    # the reveal + link card is static in #done and must contain the cockpit link
    assert 'id="done"' in h and "Open the cockpit" in h
    # showDone must guard the fragile pairing call so it can't abort the reveal
    seg = h.split("async function showDone(p)", 1)[1].split("async function ", 1)[0]
    assert "await loadPairingCode()" in seg
    # the pairing work sits inside a try/catch (guarded), and the reveal (.show) happens before it
    assert seg.index('classList.add("show")') < seg.index("await loadPairingCode()"), (
        "showDone must reveal the done card BEFORE awaiting pairing, so a pairing failure can't blank it")
    # the first lines must be guarded (no bare getElementById(...).classList that can throw)
    assert 'if(_pulse) _pulse.classList.add("ok")' in seg, "showDone's first DOM touch is not null-guarded"
