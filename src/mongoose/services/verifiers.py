from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

# Named verifiers: the SAFE way for a build step to declare success.
#
# The model does NOT supply a command to run. It selects a verifier by NAME and
# provides structured arguments (e.g. a path, a test target). Each verifier here
# is implemented BY MONGOOSE: it constructs its own argv, runs with shell=False,
# and contains every path argument to the build repo. There is no code path by
# which a model-authored string becomes a command.
#
# This is the boundary the audits asked for: model picks from a fixed menu;
# Mongoose decides what actually executes.
#
# A verify spec is a dict: {"type": "<name>", ...args}. Examples:
#   {"type": "file_exists", "path": "src/app.py"}
#   {"type": "dir_exists", "path": "tests"}
#   {"type": "pytest", "target": "tests/test_x.py"}     # target optional
#   {"type": "ruff"}                                      # path optional, defaults "."
#   {"type": "file_contains", "path": "README.md", "text": "Usage"}
#   {"type": "always_pass"}                               # for non-verifiable steps

_TIMEOUT = 600


def _contained(repo: Path, rel: str) -> Path | None:
    """Resolve a repo-relative path and ensure it stays inside the repo.
    Returns the resolved Path, or None if it escapes. Absolute paths and any '..'
    component are rejected outright (not silently neutralised by stripping the
    leading slash), so a value like '/tmp' can never slip through."""
    if not isinstance(rel, str) or not rel.strip():
        return None
    rel = rel.strip()
    if rel.startswith("/") or ".." in Path(rel).parts:
        return None
    repo_root = repo.resolve()
    try:
        p = (repo_root / rel).resolve()
        p.relative_to(repo_root)
        return p
    except (ValueError, OSError):
        return None


def _v_file_exists(repo: Path, spec: dict[str, Any]) -> tuple[bool, str]:
    p = _contained(repo, spec.get("path", ""))
    if p is None:
        return False, f"file_exists: path missing or escapes repo: {spec.get('path')!r}"
    return (p.is_file(), f"file_exists {spec['path']}: {'found' if p.is_file() else 'missing'}")


def _v_dir_exists(repo: Path, spec: dict[str, Any]) -> tuple[bool, str]:
    p = _contained(repo, spec.get("path", ""))
    if p is None:
        return False, f"dir_exists: path missing or escapes repo: {spec.get('path')!r}"
    return (p.is_dir(), f"dir_exists {spec['path']}: {'found' if p.is_dir() else 'missing'}")


def _v_file_contains(repo: Path, spec: dict[str, Any]) -> tuple[bool, str]:
    p = _contained(repo, spec.get("path", ""))
    text = spec.get("text", "")
    if p is None or not text:
        return False, "file_contains: requires a contained 'path' and non-empty 'text'"
    if not p.is_file():
        return False, f"file_contains {spec['path']}: file missing"
    try:
        body = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"file_contains: read error: {exc}"
    return (text in body, f"file_contains {spec['path']}: {'present' if text in body else 'absent'}")


def _v_pytest(repo: Path, spec: dict[str, Any]) -> tuple[bool, str]:
    # The model may name a target test path; we contain it. No target = run all.
    argv = ["python", "-m", "pytest", "-q"]
    target = spec.get("target")
    if target:
        p = _contained(repo, target)
        if p is None:
            return False, f"pytest: target escapes repo: {target!r}"
        # pass the CONTAINED repo-relative path (not the raw input)
        argv.append(str(p.relative_to(repo.resolve()).as_posix()))
    return _run(argv, repo, "pytest")


def _v_ruff(repo: Path, spec: dict[str, Any]) -> tuple[bool, str]:
    raw = spec.get("path", ".")
    if raw == ".":
        return _run(["ruff", "check", "."], repo, "ruff")
    p = _contained(repo, raw)
    if p is None:
        return False, f"ruff: path escapes repo: {raw!r}"
    return _run(["ruff", "check", str(p.relative_to(repo.resolve()))], repo, "ruff")


def _v_python_imports(repo: Path, spec: dict[str, Any]) -> tuple[bool, str]:
    """Check a module imports cleanly: `python -c 'import <module>'`, where the
    module name is validated (identifiers + dots only), so this is NOT arbitrary
    code, just an import smoke test the model can request by name."""
    module = spec.get("module", "")
    if not isinstance(module, str) or not module:
        return False, "python_imports: requires a 'module' name"
    parts = module.split(".")
    if not all(part.isidentifier() for part in parts):
        return False, f"python_imports: invalid module name {module!r}"
    return _run(["python", "-c", f"import {module}"], repo, "python_imports")


def _v_always_pass(repo: Path, spec: dict[str, Any]) -> tuple[bool, str]:
    return True, "always_pass"


def _run(argv: list[str], repo: Path, label: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=_TIMEOUT)
        return r.returncode == 0, f"{label}: exit {r.returncode}\n{(r.stdout + r.stderr)[-1500:]}"
    except subprocess.TimeoutExpired:
        return False, f"{label}: timed out"
    except FileNotFoundError as exc:
        return False, f"{label}: tool not found: {exc}"
    except Exception as exc:  # noqa: BLE001 - verifier must never crash the runner
        return False, f"{label}: error: {exc}"


VERIFIERS: dict[str, Callable[[Path, dict[str, Any]], tuple[bool, str]]] = {
    "file_exists": _v_file_exists,
    "dir_exists": _v_dir_exists,
    "file_contains": _v_file_contains,
    "pytest": _v_pytest,
    "ruff": _v_ruff,
    "python_imports": _v_python_imports,
    "always_pass": _v_always_pass,
}

VERIFIER_NAMES = tuple(VERIFIERS)


def validate_verify(spec: Any) -> str | None:
    """Return an error string if `spec` is not a valid verify spec, else None."""
    if not isinstance(spec, dict):
        return "verify must be an object like {\"type\": \"file_exists\", \"path\": \"...\"}"
    vtype = spec.get("type")
    if vtype not in VERIFIERS:
        return f"unknown verify type {vtype!r}; must be one of {', '.join(VERIFIER_NAMES)}"
    return None


def run_verifier(spec: dict[str, Any], repo: Path) -> tuple[bool, str]:
    """Run a named verifier. The model chose the type and args; Mongoose owns the
    execution. Unknown types fail closed."""
    vtype = spec.get("type")
    fn = VERIFIERS.get(vtype)
    if fn is None:
        return False, f"unknown verify type: {vtype!r}"
    return fn(repo, spec)
