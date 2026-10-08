"""Dead-code gates, built from this codebase's own bug history rather than generic linting.

Two real shipped bugs were 'designed a feature, never wired it in':
  - _INVITING_OPENERS in care_planner: a module-level constant defined and referenced nowhere,
    so care nudges never got their warm openers (caught only by a failing behavioural test).
  - approve(url)/cancelAct(url) in the desktop cockpit JS: dead functions with a STALE, less
    safe implementation than the live path, left behind by a refactor.

pyflakes catches neither class (it flags unused imports/names in a scope, not unused
module-level constants that are technically 'used' by existing, nor unreferenced JS inside
Python strings). These gates do, with zero dependencies, so they run on any machine including
the deploy gate's.
"""
from __future__ import annotations

import ast
import pathlib
import re

SRC = pathlib.Path("src/mongoose")

# Constants that are intentionally defined for external/dynamic consumption.
CONST_WHITELIST = {
    "__version__",  # read by packaging/updater via regex, not import-reference
}

# JS functions wired via mechanisms this simple scanner can't see, OR real capabilities
# deliberately kept pending wiring. Each entry must say which.
JS_WHITELIST: set[str] = {
    # Real body-data CSV import capability (posts to /body/data/ingest); built but not yet wired
    # to a file input + button on the launchpad. Kept for a dedicated wiring pass, not deleted.
    "uploadBodyData",
}


def _module_level_constants(tree: ast.Module) -> list[str]:
    names = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name) and re.fullmatch(r"_?[A-Z][A-Z0-9_]+", t.id):
                names.append(t.id)
    return names


def test_no_dead_module_level_constants():
    """Every module-level UPPER_CASE constant must be READ somewhere in src (any module).
    A constant assigned and never read anywhere is a designed-but-unwired feature or leftover."""
    all_text = {p: p.read_text() for p in SRC.rglob("*.py")}
    # Corpus includes tests: a constant consumed only by a test is a PINNED CONTRACT (the test
    # will fail if the constant drifts), which is a legitimate use, not dead code.
    corpus = "\n".join(all_text.values()) + "\n".join(
        q.read_text() for q in pathlib.Path("tests").glob("*.py"))
    dead = []
    for path, text in all_text.items():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for name in _module_level_constants(tree):
            if name in CONST_WHITELIST:
                continue
            # Occurrences beyond its own assignment lines: crude but effective word count
            # across the whole package (covers cross-module imports too).
            uses = len(re.findall(rf"\b{re.escape(name)}\b", corpus))
            assigns = len(re.findall(rf"^\s*{re.escape(name)}\s*(?::[^=]+)?=", text, re.M))
            if uses <= assigns:
                dead.append(f"{path}:{name}")
    assert not dead, (
        "Dead module-level constants (defined, never read anywhere in src). Wire them in or "
        "delete them; if one is consumed externally/dynamically, whitelist it WITH a comment "
        f"saying how: {dead}")


def _js_function_names(html: str) -> list[str]:
    return re.findall(r"(?:^|\n)\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", html)


def _dead_js_functions(html: str) -> list[str]:
    dead = []
    for name in _js_function_names(html):
        if name in JS_WHITELIST:
            continue
        # One occurrence = only its own definition. onclick attributes, addEventListener
        # references, and direct calls all add occurrences.
        if len(re.findall(rf"\b{re.escape(name)}\b", html)) <= 1:
            dead.append(name)
    return dead


def test_no_dead_functions_in_desktop_cockpit_js():
    from mongoose.cockpit import cockpit_html
    dead = _dead_js_functions(cockpit_html())
    assert not dead, f"Dead JS functions in desktop cockpit (defined, never referenced): {dead}"


def test_no_dead_functions_in_phone_cockpit_js():
    from mongoose.cockpit import phone_cockpit_html
    dead = _dead_js_functions(phone_cockpit_html())
    assert not dead, f"Dead JS functions in phone cockpit (defined, never referenced): {dead}"


def test_no_dead_functions_in_launchpad_js():
    from mongoose.cockpit import device_launchpad_html
    dead = _dead_js_functions(device_launchpad_html())
    assert not dead, f"Dead JS functions in launchpad (defined, never referenced): {dead}"


def test_gate_would_have_caught_the_historical_bugs():
    """Self-test against the two shipped bugs' exact shapes, so a future weakening of the
    scanner fails loudly here rather than silently letting the class back in."""
    fake_py = "SOME_CONST = 1\n_OPENERS = ('a', 'b')\nused = _OPENERS[0]\n"
    tree = ast.parse(fake_py)
    consts = _module_level_constants(tree)
    assert "SOME_CONST" in consts and "_OPENERS" in consts
    # SOME_CONST: 1 use == 1 assign -> dead; _OPENERS: 2 uses > 1 assign -> live
    assert len(re.findall(r"\bSOME_CONST\b", fake_py)) == 1
    assert len(re.findall(r"\b_OPENERS\b", fake_py)) == 2
    fake_js = "function live(){} live();\nasync function deadOne(){ return 1; }\n"
    assert _dead_js_functions(fake_js) == ["deadOne"]
