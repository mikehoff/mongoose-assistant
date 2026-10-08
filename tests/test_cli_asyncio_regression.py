"""Regression tests for the benchmark/install post-mortem.

The asyncio scoping bug crashed `mongoose benchmark` (and any other main() branch
that called asyncio.run before a local `import asyncio`) with UnboundLocalError. The
existing benchmark tests called the internal functions directly, so they never
exercised main()'s dispatch and missed it. These tests close that gap and guard
against reintroduction."""
import ast
import sys
from pathlib import Path



CLI_PATH = Path(__file__).resolve().parent.parent / "src" / "mongoose" / "cli.py"


def test_main_has_no_local_asyncio_import():
    """asyncio must be imported at module level only. A local `import asyncio` inside
    main() makes asyncio a local for the whole function, so earlier asyncio.run(...)
    calls raise UnboundLocalError. This guards against that exact reintroduction."""
    tree = ast.parse(CLI_PATH.read_text())
    main = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert main is not None
    local_async_imports = [
        n.lineno for n in ast.walk(main)
        if isinstance(n, ast.Import) and any(a.name == "asyncio" for a in n.names)
    ]
    assert not local_async_imports, (
        f"local `import asyncio` in main() at lines {local_async_imports}; "
        "remove them and use the module-level import")


def test_no_function_imports_asyncio_after_using_it():
    """Generalises the rule: for ANY function, a local `import asyncio` must not come
    after an asyncio.run() use (that's the UnboundLocalError pattern)."""
    tree = ast.parse(CLI_PATH.read_text())
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        local_imports = [n.lineno for n in ast.walk(fn)
                         if isinstance(n, ast.Import) and any(a.name == "asyncio" for a in n.names)]
        uses = [n.lineno for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "run" and getattr(n.func.value, "id", "") == "asyncio"]
        if local_imports and uses and min(uses) < max(local_imports):
            offenders.append(fn.name)
    assert not offenders, f"functions import asyncio after using it: {offenders}"


def test_benchmark_cli_dispatches_without_unbound_error(monkeypatch):
    """Drive `mongoose benchmark` through main() with a mocked run_benchmark, proving
    the dispatch path reaches the command (no UnboundLocalError before it). This is
    the CLI-level coverage the internal benchmark tests didn't provide."""
    import mongoose.cli as cli

    called = {}

    async def fake_run_benchmark(models, **kw):
        called["models"] = models
        called["kw"] = kw
        return {"ok": True}

    # patch where it's imported inside the handler
    import mongoose.benchmark as bench
    monkeypatch.setattr(bench, "run_benchmark", fake_run_benchmark)
    monkeypatch.setattr(sys, "argv",
                        ["mongoose", "benchmark", "mock-model",
                         "--rounds", "1", "--repeats", "1", "--no-cold", "--no-pull"])
    rc = cli.main()
    assert called.get("models") == ["mock-model"]
    assert rc in (None, 0)


def test_all_asyncio_run_branches_reachable():
    """Every command branch that calls asyncio.run should be dispatchable: confirm
    asyncio resolves to the module at the point main() runs (sanity that the global
    import is the one in scope)."""
    import mongoose.cli as cli
    import asyncio as real_asyncio
    assert cli.asyncio is real_asyncio


# --- the same bug class for `os` (the server-watchdog --install post-mortem) -------------------------
# `server-watchdog --install` printed "Installed the server watchdog launch agent: ..." and then
# crashed on `uid = os.getuid()` with "cannot access local variable 'os'", because a bare
# `import os` elsewhere in main() made os a function-local. Under the installer's `set -e -o pipefail`,
# the resulting non-zero exit aborted the whole bootstrap right before it marked the install ready, so
# a fully-working install (smoke test 14/14) was reported as failed. These guard the exact reintroduction.

def test_main_has_no_local_os_import():
    """os must be imported at module level only. A local `import os` inside main() makes os a local for
    the whole function, so any os.* use on a branch that didn't run the import first raises
    UnboundLocalError (this is what broke `server-watchdog --install`)."""
    tree = ast.parse(CLI_PATH.read_text())
    main = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert main is not None
    local_os_imports = [
        n.lineno for n in ast.walk(main)
        if isinstance(n, ast.Import) and any(a.name == "os" and a.asname is None for a in n.names)
    ]
    assert not local_os_imports, (
        f"bare `import os` in main() at lines {local_os_imports}; remove them and use the "
        "module-level import (an `import os as _os` in a single branch is fine)")


def test_os_resolves_to_module_in_cli():
    """Sanity: os is the stdlib module at module scope, so every main() branch using os.* works."""
    import mongoose.cli as cli
    import os as real_os
    assert cli.os is real_os


def test_no_cli_function_imports_a_stdlib_name_after_using_it():
    """Generalised guard across the WHOLE module: for any function, a bare local `import <mod>` must not
    come after a use of that name. That ordering is the UnboundLocalError trap, regardless of which
    stdlib module it is. Catches the os/asyncio/sys/etc. family in one rule."""
    STDLIB = {"os", "sys", "asyncio", "json", "time", "re", "subprocess", "shutil", "pathlib"}
    tree = ast.parse(CLI_PATH.read_text())
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for mod in STDLIB:
            imp_lines = [n.lineno for n in ast.walk(fn)
                         if isinstance(n, ast.Import)
                         and any(a.name == mod and a.asname is None for a in n.names)]
            if not imp_lines:
                continue
            use_before = [n.lineno for n in ast.walk(fn)
                          if isinstance(n, ast.Name) and n.id == mod
                          and isinstance(n.ctx, ast.Load) and n.lineno < min(imp_lines)]
            if use_before:
                offenders.append(f"{fn.name}: uses {mod!r} at {sorted(set(use_before))} before local import at {min(imp_lines)}")
    assert not offenders, "UnboundLocal shadow risk:\n  " + "\n  ".join(offenders)


def test_watchdog_install_dispatches_without_unbound_error(monkeypatch, tmp_path):
    """Drive `server-watchdog --install` through main(), proving the handler reaches os.getuid()
    without UnboundLocalError and returns success. The agent-writing step is mocked so the test does
    not touch the real LaunchAgents dir."""
    import mongoose.cli as cli

    plist = tmp_path / "com.mongoose.server-watchdog.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(cli, "_wd_install_agent", lambda: str(plist))
    monkeypatch.setattr(sys, "argv", ["mongoose", "server-watchdog", "--install"])
    # Must not raise UnboundLocalError; must report success (return None or 0), NOT the error path's 1.
    rc = cli.main()
    assert rc in (None, 0), f"watchdog --install returned {rc!r} (the error path returns 1)"
