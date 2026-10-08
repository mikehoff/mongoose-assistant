"""Release gate: every embedded page <script> must be valid JavaScript with no undefined references.

This is the test that would have caught the dead-browser bugs on day one. The install/welcome/cockpit
pages are large JavaScript programs embedded in Python strings. Nothing validated them, so:
  * `esc()` was called six times in the install page but never defined (dead screen at 100%);
  * `postAction()` was called by the install page's enablePhone() but only defined on the cockpit page
    (dead "Allow phone access" button).
Both shipped through a fully-green Python suite because no test parsed the JavaScript. A pure parser
would not have caught them either (calling an undefined function is a runtime ReferenceError, not a
syntax error), so the checker (tools/jscheck/check_js.js, using vendored acorn) does scope analysis:
it flags any identifier that is referenced but never declared in scope and is not a known global.

This gate extracts every inline <script> from each rendered page and runs the checker on it. A failure
here means a page would throw in the browser. The checker is zero-network (acorn is vendored), so this
runs anywhere the Python suite does, provided Node is available; if Node is absent the test skips with a
clear message rather than silently passing.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / "tools" / "jscheck" / "check_js.js"

# inline <script> blocks only (skip ones with a src= attribute, which load external/static files)
_SCRIPT_RE = re.compile(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL)


def _node() -> str | None:
    return shutil.which("node")


def _check(src: str) -> dict:
    """Run the JS checker on a source string; return its parsed JSON verdict."""
    proc = subprocess.run(
        [_node(), str(CHECKER)],
        input=src, capture_output=True, text=True, timeout=30,
    )
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "errors": [{"type": "checker-output", "message": proc.stdout + proc.stderr}]}


def _scripts_of(html: str) -> list[str]:
    return [b for b in _SCRIPT_RE.findall(html) if b.strip()]


def _rendered_pages() -> dict[str, str]:
    """Every page renderer that ships embedded JS the browser will run. Auto-discovered from cockpit.py
    (every zero-arg `*_html()` function) so a newly added page is covered automatically rather than
    silently escaping the gate."""
    import inspect
    from mongoose import cockpit as C
    pages: dict[str, str] = {}
    for name, fn in inspect.getmembers(C, inspect.isfunction):
        if not name.endswith("_html"):
            continue
        if fn.__module__ != C.__name__:
            continue
        # only call the ones that take no required arguments (page renderers); skip helpers like
        # functions that need a request/state argument.
        sig = inspect.signature(fn)
        if any(p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
               for p in sig.parameters.values()):
            continue
        try:
            html = fn()
        except Exception:
            continue
        if isinstance(html, str) and "<script" in html:
            pages[name[:-5] or name] = html  # strip the _html suffix for a clean id
    return pages


requires_node = pytest.mark.skipif(_node() is None, reason="Node.js not available to run the JS checker")


@requires_node
def test_checker_self_check_catches_undefined_and_passes_valid():
    """Prove the checker itself works before trusting it on the real pages: it must FLAG a call to an
    undefined function and PASS the same code once the function is defined."""
    bad = _check('function r(){ var e=document.getElementById("x"); e.innerHTML=esc(1); } r();')
    assert bad["ok"] is False
    assert any(e.get("name") == "esc" for e in bad["errors"]), bad
    good = _check('function esc(s){return String(s);} function r(){ esc(1); } r();')
    assert good["ok"] is True, good
    # typeof-guarded optional functions must NOT be flagged (the feature-detection idiom)
    guarded = _check('if(typeof maybe==="function"){ maybe(); }')
    assert guarded["ok"] is True, guarded
    # a real syntax error must be caught
    broken = _check("function ( { return 1 }")
    assert broken["ok"] is False and broken["errors"][0]["type"] == "syntax"


@requires_node
@pytest.mark.parametrize("page", list(_rendered_pages().keys()))
def test_every_embedded_page_script_is_valid_js(page):
    """Each inline <script> on each page must parse and have no undefined references. A failure means
    the page throws in the browser (the dead-screen class)."""
    html = _rendered_pages()[page]
    blocks = _scripts_of(html)
    assert blocks, f"{page} has no inline scripts to check (did the page change shape?)"
    problems = []
    for i, block in enumerate(blocks):
        verdict = _check(block)
        if not verdict.get("ok"):
            for e in verdict.get("errors", []):
                loc = f"line {e.get('line')}" if e.get("line") else ""
                problems.append(f"{page} script #{i}: {e.get('type')} {e.get('name', '')} {loc} , {e.get('message', '')}".strip())
    assert not problems, "Embedded JS would throw in the browser:\n" + "\n".join(problems)


@requires_node
def test_install_page_defines_the_helpers_it_calls():
    """Targeted regression for the exact bugs that shipped: the install page must define esc() and
    postAction(), because it calls them. (The broad test above already covers this, but this names the
    specific failures so a regression is unmistakable.)"""
    from mongoose.cockpit import install_html
    html = install_html()
    script = _scripts_of(html)[0]
    verdict = _check(script)
    assert verdict["ok"], verdict
    # belt and braces: the definitions are literally present
    assert "function esc(" in script
    assert "function postAction(" in script
