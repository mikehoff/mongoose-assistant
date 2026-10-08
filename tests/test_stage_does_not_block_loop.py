"""Regression guard: staging a build must NOT freeze the server's event loop.

check_update_stage runs `compileall` + `pyflakes` as subprocesses (up to 90s each). If the build
stage/deploy handlers run that directly in their async body it blocks the WHOLE event loop, so every
endpoint including /health times out until staging finishes , the "Mongoose unresponsive right after
staging" wedge seen in real use. The fix runs the blocking stage/check/apply via asyncio.to_thread.

This guards it at the source level: each build handler that does staging work must hand it to a thread.
A full live-concurrency assertion against ASGITransport proved flaky (it could pass with OR without the
bug), so rather than ship a test that gives false confidence we assert the structural invariant that
actually prevents the freeze: the handler delegates to to_thread and never calls the blocking updater
functions at the top level of its async body.
"""
from __future__ import annotations

import ast
import inspect

BLOCKING = {"check_update_stage", "stage_update_candidate", "apply_update_stage"}


def _top_level_blocking_calls(fn) -> list[str]:
    """Blocking updater calls made directly in the async function body (NOT inside a nested def, which
    is what gets handed to to_thread). We walk only statements at the function's own body depth, not
    into nested FunctionDef/Lambda , those are the threaded workers and are fine."""
    src = inspect.getsource(fn)
    tree = ast.parse(src)
    func = tree.body[0]
    offenders: list[str] = []

    def walk(node, inside_nested: bool):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                # body of a nested def/lambda is the threaded worker , skip (it runs off-loop)
                continue
            if isinstance(child, ast.Call):
                f = child.func
                name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
                if name in BLOCKING:
                    offenders.append(name)
            walk(child, inside_nested)

    walk(func, False)
    return offenders


def _uses_to_thread(fn) -> bool:
    return "to_thread" in inspect.getsource(fn)


def test_build_stage_handler_does_not_block_loop():
    import mongoose.app as A
    fn = A.build_stage_endpoint
    assert _uses_to_thread(fn), "build_stage_endpoint must delegate blocking stage/check to a thread"
    off = _top_level_blocking_calls(fn)
    assert not off, f"build_stage_endpoint calls blocking fns on the event loop: {sorted(set(off))}"


def test_build_deploy_handler_does_not_block_loop():
    import mongoose.app as A
    fn = A.build_deploy_endpoint
    assert _uses_to_thread(fn), "build_deploy_endpoint must delegate blocking stage/check/apply to a thread"
    off = _top_level_blocking_calls(fn)
    assert not off, f"build_deploy_endpoint calls blocking fns on the event loop: {sorted(set(off))}"


def test_build_ingest_handler_does_not_block_loop():
    import mongoose.app as A
    fn = A.build_ingest_endpoint
    assert _uses_to_thread(fn), "build_ingest_endpoint must delegate blocking work to a thread"
    off = _top_level_blocking_calls(fn)
    assert not off, f"build_ingest_endpoint calls blocking fns on the event loop: {sorted(set(off))}"
