"""Guard against a JavaScript syntax error shipping in any embedded HTML page.

A single syntax error in the cockpit's inline <script> stops the WHOLE script parsing, so the page loads
the logo, sits at "checking", and renders no tabs , while every backend endpoint returns 200 and every
Python test passes. That failure is invisible to server-side tests, so we check it explicitly: extract
each embedded page's script blocks and require them to parse. Uses node if available; otherwise skips
(so CI without node still runs) but flags clearly.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile

import pytest


def _html_pages():
    import mongoose.cockpit as ck
    return {k: v for k, v in vars(ck).items()
            if isinstance(v, str) and v.lstrip().startswith("<!DOCTYPE")}


def _inline_assets(html: str) -> str:
    """Re-inline externalised /ui-asset CSS/JS so content and parse checks see the FULL page. The serve
    layer now lifts the big <style>/<script> blocks into content-hashed cached files, but they are
    byte-identical (just relocated), so for verification we stitch them back in."""
    from mongoose.cockpit import UI_ASSETS
    html = re.sub(r'<script src="/ui-asset/([0-9a-f]+)\.js"></script>',
                  lambda m: "<script>" + UI_ASSETS.get(m.group(1), ("", ""))[0] + "</script>", html)
    html = re.sub(r'<link rel="stylesheet" href="/ui-asset/([0-9a-f]+)\.css">',
                  lambda m: "<style>" + UI_ASSETS.get(m.group(1), ("", ""))[0] + "</style>", html)
    return html


def _script_blocks(html: str):
    return [s for s in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL) if s.strip()]


_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node not available to syntax-check JS")
def test_every_embedded_page_script_parses():
    pages = _html_pages()
    assert pages, "expected at least the cockpit HTML page"
    failures = []
    for name, html in pages.items():
        for i, block in enumerate(_script_blocks(html)):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(block)
                path = f.name
            r = subprocess.run([_NODE, "--check", path], capture_output=True, text=True)
            if r.returncode != 0:
                first = (r.stderr.strip().splitlines() or ["?"])[0]
                failures.append(f"{name} block {i}: {first}")
    assert not failures, "JavaScript syntax errors in embedded pages:\n" + "\n".join(failures)


def test_cockpit_has_a_script_block():
    # sanity: the cockpit page must actually contain inline JS (else the test above is vacuous)
    import mongoose.cockpit as ck
    assert _script_blocks(ck.COCKPIT_HTML), "cockpit page has no <script> block"


def _generated_html_pages():
    """Pages built by a FUNCTION (not a module-level constant) , e.g. guest_invite_html(),
    cockpit_html(), the launchpad and phone cockpit. The constant-only scan above misses these,
    which is exactly how a broken esc() shipped on the guest invite page."""
    import mongoose.cockpit as ck
    out = {}
    for name, obj in vars(ck).items():
        if callable(obj) and name.endswith("_html"):
            try:
                html = _inline_assets(obj())  # inline externalised assets so the big JS is parse-checked
            except Exception:
                continue
            if isinstance(html, str) and "<script" in html:
                out[name + "()"] = html
    return out


@pytest.mark.skipif(_NODE is None, reason="node not available to syntax-check JS")
def test_every_generated_page_script_parses():
    pages = _generated_html_pages()
    assert "guest_invite_html()" in pages, "guest invite page builder should be discovered"
    failures = []
    for name, html in pages.items():
        for i, block in enumerate(_script_blocks(html)):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(block)
                path = f.name
            r = subprocess.run([_NODE, "--check", path], capture_output=True, text=True)
            if r.returncode != 0:
                first = (r.stderr.strip().splitlines() or ["?"])[0]
                failures.append(f"{name} block {i}: {first}")
    assert not failures, "JavaScript syntax errors in generated pages:\n" + "\n".join(failures)


@pytest.mark.skipif(_NODE is None, reason="node not available to syntax-check JS")
def test_service_worker_js_parses():
    """The PWA service worker is JS served from app.py; a syntax error there silently breaks offline
    shell + install for every device."""
    from starlette.testclient import TestClient
    from mongoose.app import app
    body = TestClient(app).get("/sw.js").text
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(body)
        path = f.name
    r = subprocess.run([_NODE, "--check", path], capture_output=True, text=True)
    assert r.returncode == 0, "service worker JS syntax error: " + (r.stderr.strip()[:200])


def test_no_bare_nonce_call_in_cockpit_js():
    """Regression for the Build/deploy tab crash: `const n=nonce();` called an undefined `nonce()`
    (only `NONCE`/`ensureNonce`/`getNonce` exist), throwing ReferenceError on render so the stage box
    and deploy buttons never appeared. node --check passes (valid syntax) so the syntax test missed it.
    If the cockpit JS calls `nonce(` at all, a `function nonce(` / `nonce =` must define it."""
    import mongoose.cockpit as ck
    html = next(v for v in vars(ck).values()
                if isinstance(v, str) and v.lstrip().startswith("<!DOCTYPE") and "loadBuild" in v)
    blocks = "\n".join(_script_blocks(html))
    if re.search(r"(?<![\w$.])nonce\s*\(", blocks):
        defines_nonce = bool(re.search(r"\bfunction\s+nonce\s*\(", blocks) or
                             re.search(r"\b(?:const|let|var)\s+nonce\s*=", blocks))
        assert defines_nonce, (
            "cockpit JS calls nonce() but never defines it , this is the ReferenceError that crashed "
            "the Build/deploy tab on render. Use getNonce()/postAction.")


def test_every_tab_loader_is_defined():
    """Every loader wired into the tab-switch map (loaders={overview:loadOverview,...}) must be a
    defined function. If the map references a name with no `function name(` / `const name =`, switching
    to that tab throws ReferenceError and renders nothing , the class of bug that hit the Build tab via
    nonce(). node --check can't see it (valid syntax). Scoped to the loaders map so it is precise and
    never false-positives on guarded/optional calls."""
    import mongoose.cockpit as ck
    page = next(v for v in vars(ck).values()
                if isinstance(v, str) and v.lstrip().startswith("<!DOCTYPE")
                and "loaders={overview:loadOverview" in v)
    blocks = "\n".join(_script_blocks(page))
    # Extract the loaders map object literal and pull the function names it maps to.
    m = re.search(r"loaders\s*=\s*\{(.*?)\}", blocks, re.DOTALL)
    assert m, "could not find the loaders map"
    body = m.group(1)
    # values are like  overview:loadOverview,  chat:chatSetup,  search:()=>{}
    targets = re.findall(r":\s*([A-Za-z_$][\w$]*)\s*[,}]", body)
    missing = []
    for name in sorted(set(targets)):
        defined = bool(re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\(", blocks) or
                       re.search(r"\b(?:const|let|var)\s+" + re.escape(name) + r"\s*=", blocks))
        if not defined:
            missing.append(name)
    assert not missing, (
        "the tab-switch loaders map points at these undefined functions (ReferenceError on tab "
        "switch):\n  " + ", ".join(missing))


def test_phone_surface_onclick_handlers_are_defined_on_their_own_page():
    """The two phone surfaces (launchpad and phone cockpit) are SEPARATE pages with SEPARATE served JS
    bundles. A function defined in one but referenced by an onclick in the other passes node --check on
    each bundle (each is valid in isolation) yet throws ReferenceError at tap time, so the button
    silently does nothing. That is exactly how the phone 'Check and apply update' button broke:
    doDeviceUpdate was defined only in the launchpad shell but called from the phone cockpit. Assert
    every top-level function an inline handler calls is defined in the SAME page's scripts."""
    import mongoose.cockpit as ck
    pages = {
        "launchpad": _inline_assets(ck.device_launchpad_html()),
        "phone_cockpit": _inline_assets(ck.phone_cockpit_html()),
    }
    _DEF = [r'function\s+([A-Za-z_$][\w$]*)', r'([A-Za-z_$][\w$]*)\s*=\s*function',
            r'([A-Za-z_$][\w$]*)\s*=\s*async\s+function', r'async\s+function\s+([A-Za-z_$][\w$]*)',
            r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=', r'window\.([A-Za-z_$][\w$]*)\s*=',
            r'([A-Za-z_$][\w$]*)\s*=\s*\([^)]*\)\s*=>', r'([A-Za-z_$][\w$]*)\s*=\s*async\s*\([^)]*\)\s*=>']
    _BUILTINS = set((
        "return if for while switch new typeof void delete instanceof in of do else try catch throw "
        "alert confirm prompt esc token api j fetch JSON Date Math parseInt parseFloat String Number "
        "Boolean Array Object encodeURIComponent decodeURIComponent setTimeout setInterval localStorage "
        "console navigator Promise Set Map URLSearchParams FormData Blob URL atob btoa isNaN").split())
    # Words that survive string-stripping only because they sit inside template-concatenated markup, not
    # as real calls (verified: no literal `word(` handler exists). Benign; keep the gate from false alarms.
    _TEMPLATE_ARTIFACTS = {"phone", "unavailable", "yet"}
    _allow = _BUILTINS | _TEMPLATE_ARTIFACTS

    def _strip_strings(s):
        s = re.sub(r'`(?:\\.|[^`\\])*`', ' ', s)
        s = re.sub(r"'(?:\\.|[^'\\])*'", ' ', s)
        s = re.sub(r'"(?:\\.|[^"\\])*"', ' ', s)
        return s

    problems = {}
    for name, html in pages.items():
        scripts = "\n".join(_script_blocks(html))
        defs = set()
        for pat in _DEF:
            defs |= set(re.findall(pat, scripts))
        handlers = (re.findall(r'on[a-z]+\s*=\s*"([^"]*)"', html)
                    + re.findall(r"on[a-z]+\s*=\s*'([^']*)'", html))
        called = set()
        for h in handlers:
            for fn in re.findall(r'(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(', _strip_strings(h)):
                called.add(fn)
        missing = sorted(c for c in called if c not in defs and c not in _allow)
        if missing:
            problems[name] = missing
    assert not problems, (
        "phone onclick handlers call functions not defined on the SAME page (dead buttons: "
        "ReferenceError at tap, button does nothing): " + repr(problems))


def test_cockpit_setup_action_handlers_are_defined():
    """The cockpit's own Setup-tab buttons (guest access via the Cloudflare tunnel, reboot persistence,
    LAN access, remote-access repair, body import) call handlers that were only ever DEFINED on the
    install and launchpad pages, so on /cockpit they threw ReferenceError and silently did nothing.
    cockpit_html() now injects them from those pages (one source of truth); assert every one is present
    in the served page so that load-bearing guest-access flow cannot silently go dead again."""
    from mongoose.cockpit import cockpit_html
    html = _inline_assets(cockpit_html())
    for fn in ("enablePhone", "serviceInstall", "tunnelInstall", "tunnelLogin",
               "tunnelProvision", "repairRemoteAccess", "uploadBodyData"):
        assert re.search(r"\basync function " + fn + r"\s*\(", html), (
            fn + "() is not defined in the served cockpit page (dead Setup-tab button)")


def test_nav_grouping_covers_every_tab_exactly_once():
    """Progressive-disclosure nav: every tab in TABS must appear exactly once across
    PRIMARY_TABS + MORE_GROUPS, with no tab missing, duplicated, or unknown. This guards
    against a tab silently vanishing from the UI when someone edits the grouping."""
    import re
    from mongoose.cockpit import cockpit_html
    html = _inline_assets(cockpit_html())

    def _arr(name):
        m = re.search(name + r"\s*=\s*\[(.*?)\];", html, re.S)
        assert m, f"{name} not found in cockpit JS"
        return re.findall(r'"([a-z]+)"', m.group(1))

    tabs = _arr("const TABS")
    primary = _arr("const PRIMARY_TABS")
    # MORE_GROUPS is an array of objects with tabs:[...]; pull every tabs:[...] list
    more_block = re.search(r"const MORE_GROUPS\s*=\s*\[(.*?)\];", html, re.S)
    assert more_block, "MORE_GROUPS not found"
    more = re.findall(r'tabs:\[(.*?)\]', more_block.group(1))
    more_tabs = []
    for grp in more:
        more_tabs += re.findall(r'"([a-z]+)"', grp)

    combined = primary + more_tabs
    # 13 top-level tabs: Models/Benchmarks/Logs -> System, Voicemail -> RTM, and Music onboarding is
    # chat-only now (no Music tab anywhere).
    assert len(tabs) == 13, f"expected 13 tabs, got {len(tabs)}: {tabs}"
    assert sorted(combined) == sorted(tabs), (
        f"nav grouping mismatch. missing={set(tabs)-set(combined)}, "
        f"unknown={set(combined)-set(tabs)}, dupes={[t for t in combined if combined.count(t)>1]}")
    assert len(combined) == len(set(combined)), "a tab appears more than once in the nav grouping"


def test_nav_has_progressive_disclosure_helpers():
    """The grouped nav must ship the helpers that let any tab be activated regardless of
    whether it's primary or under More (so deep-links and programmatic show() calls work)."""
    from mongoose.cockpit import cockpit_html
    html = _inline_assets(cockpit_html())
    assert "function activateTab(" in html, "activateTab helper missing"
    assert "function _btnForTab(" in html, "_btnForTab helper missing"
    # deep-link landing must go through activateTab, not index-based nav.children matching
    assert "activateTab(landing)" in html, "deep-link should use activateTab"


def test_ribbon_search_indexes_load_bearing_real_user_terms():
    """The ribbon is navigation search, not document search, so it must surface the exact things a
    real owner asks for when lost: guests, scheduler memory relief, RTM build provenance, v3 peers,
    and document/email/voicemail search."""
    import mongoose.cockpit as ck
    blob = ck.COCKPIT_HTML + "\n" + ck.PHONE_COCKPIT_HTML
    for term in [
        "Generate guest invite",
        "free memory",
        "RTM source of truth",
        "RTM build plan",
        "Protocol v3 coordination",
        "Document/email search",
        "Voicemail search",
    ]:
        assert term in blob


# Start chars that GLUE a new line onto the previous statement at runtime: '(' (call) and '[' (index).
# Backtick (tagged template) is deliberately excluded , real multi-line HTML templates start lines with
# it constantly and it is never the footgun in practice.
_ASI_HAZARD_START = "(["
# Chars that END an expression (so a following '(' / '[' would be read as call/index, not a new
# statement). '}' is excluded on purpose: a line ending in '}' is almost always a block close, and
# '}\\n(' is safe; including it only produced false positives on if/for/function blocks.
_ASI_EXPR_END = set(")]" + "0123456789$_'\"`"
                    + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _asi_offenders(js: str):
    """Find ASI concatenation hazards in code context: a line whose first code char is ( [ or ` while
    the previous code line ends an expression, so JS glues them (foo()\\n(bar) -> foo()(bar)). Tracks
    strings, template literals, and // and /* */ comments so it never false-positives on their contents.
    Returns [(prev_snippet, next_snippet)]."""
    offs, in_block, in_tmpl, prev_code = [], False, False, ""
    for raw in js.split("\n"):
        out, i, L = [], 0, len(raw)
        while i < L:
            if in_block:
                k = raw.find("*/", i)
                if k < 0:
                    i = L
                    continue
                i, in_block = k + 2, False
                continue
            if in_tmpl:
                k = raw.find("`", i)
                if k < 0:
                    i = L
                    continue
                i, in_tmpl = k + 1, False
                out.append("`")
                continue
            two = raw[i:i + 2]
            if two == "//":
                break
            if two == "/*":
                k = raw.find("*/", i + 2)
                if k < 0:
                    in_block = True
                    i = L
                    continue
                i = k + 2
                continue
            ch = raw[i]
            if ch in "'\"":
                q = ch
                i += 1
                while i < L and raw[i] != q:
                    i += 2 if raw[i] == "\\" else 1
                i += 1
                out.append(q)
                continue
            if ch == "`":
                i += 1
                while i < L and raw[i] != "`":
                    i += 2 if raw[i] == "\\" else 1
                if i >= L:
                    in_tmpl = True
                    out.append("`")
                    i = L
                    continue
                i += 1
                out.append("`")
                continue
            out.append(ch)
            i += 1
        code = "".join(out).strip()
        if not code:
            continue
        if code[0] in _ASI_HAZARD_START and prev_code and prev_code[-1] in _ASI_EXPR_END \
                and not prev_code.endswith("=>"):
            offs.append((prev_code[-45:], code[:45]))
        prev_code = code
    return offs


def test_no_asi_init_crash_hazard_in_any_page_js():
    """The safeguard for the class of bug that a syntax check CANNOT catch: JS that PARSES but crashes
    at load, halting the whole page (stuck 'checking' screen / dead buttons). The concrete instance was
    the '#say= IIFE' that glued to `ensureNamed()` via ASI (`ensureNamed()(...)`), a load-time TypeError
    that silently broke private-chat send for everyone. Since `node` is often absent (the parse tests
    skip), THIS pure-Python check is the real guard. Fix any hit by starting the line with a `;`."""
    offenders = []
    for name, html in _html_pages().items():
        for block in _script_blocks(_inline_assets(html)):
            for prev, nxt in _asi_offenders(block):
                offenders.append((name, prev, nxt))
    assert not offenders, (
        "ASI init-crash hazard (a line starting with ( [ or ` after an expression glues to it at "
        "runtime; prefix the line with ';'):\n"
        + "\n".join(f"  {n}: ...{p!r}  <NEWLINE>  {c!r}..." for n, p, c in offenders[:8]))


def _defined_and_called(scripts_text: str):
    """(defined names, called names) for a page's concatenated, string/comment-stripped JS."""
    s = re.sub(r'`(?:\\.|[^`\\])*`', ' ', scripts_text)
    s = re.sub(r"'(?:\\.|[^'\\])*'", ' ', s)
    s = re.sub(r'"(?:\\.|[^"\\])*"', ' ', s)
    s = re.sub(r'//[^\n]*', ' ', s)
    s = re.sub(r'/\*.*?\*/', ' ', s, flags=re.DOTALL)
    _DEF = [r'function\s+([A-Za-z_$][\w$]*)', r'([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function',
            r'(?:async\s+)?function\s+([A-Za-z_$][\w$]*)', r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=',
            r'window\.([A-Za-z_$][\w$]*)\s*=', r'([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>']
    defs = set()
    for p in _DEF:
        defs |= set(re.findall(p, s))
    called = set(re.findall(r'(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(', s))
    return defs, called


def _near(a: str, b: str) -> bool:
    """a and b are 'the same function, mistyped': identical after stripping a leading underscore, or a
    single-character insert/delete/substitution (Levenshtein <= 1). Catches _pollChatJob vs pollChatJob."""
    if a == b:
        return False
    if a.lstrip("_") == b or b.lstrip("_") == a or a.lstrip("_") == b.lstrip("_"):
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    # Levenshtein <= 1
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    lo, hi = (a, b) if len(a) < len(b) else (b, a)
    i = j = 0; skipped = False
    while i < len(lo) and j < len(hi):
        if lo[i] == hi[j]:
            i += 1; j += 1
        elif skipped:
            return False
        else:
            skipped = True; j += 1
    return True


def test_no_dead_function_call_typos_in_served_js():
    """The class that shipped _pollChatJob (a typo for pollChatJob) into the voice reply path: a call to a
    function that does NOT exist, but whose CORRECT name is defined right there. node --check passes (valid
    syntax); it throws ReferenceError only at runtime, on the exact slow-reply path a quick test never hits.
    Precise + low-noise: only flag a called-but-undefined name when a near-identical name IS defined on the
    same page (typo signal), so builtins/DOM globals (Date, fetch, Promise) never false-positive."""
    import mongoose.cockpit as ck
    pages = {
        "COCKPIT_HTML": ck.COCKPIT_HTML,
        "device_launchpad_html()": _inline_assets(ck.device_launchpad_html()),
        "phone_cockpit_html()": _inline_assets(ck.phone_cockpit_html()),
    }
    _KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "typeof", "void",
                 "delete", "instanceof", "in", "of", "do", "else", "try", "throw", "new", "var", "let",
                 "const", "await", "yield", "case", "with"}
    offenders = {}
    for name, html in pages.items():
        raw = "\n".join(_script_blocks(html))
        defs, called = _defined_and_called(raw)
        typos = set()
        # (a) The exact _pollChatJob class, detected on the RAW js (robust: the fragile string/template
        # strip can swallow a call site, but a bare `_X(` where X is a defined function and _X is not is
        # an unambiguous typo). This is the one that shipped the dead voice-reply call.
        for m in set(re.findall(r'(?<![\w.$])(_[A-Za-z][\w$]{3,})\s*\(', raw)):
            if m not in defs and m.lstrip("_") in defs:
                typos.add(f"{m}() -> did you mean {m.lstrip('_')}()?")
        # (b) General single-edit near-match on the stripped call set (best-effort; long names only, so
        # short names/keywords never false-positive).
        for c in sorted(called):
            if c in defs or c in _KEYWORDS or len(c) < 6:
                continue
            near = [d for d in defs if len(d) >= 6 and _near(c, d)]
            if near:
                typos.add(f"{c}() -> did you mean {near[0]}()?")
        if typos:
            offenders[name] = sorted(typos)
    assert not offenders, (
        "dead function-call typos in served JS (ReferenceError at runtime, invisible to node --check):\n"
        + "\n".join(f"  {n}: {t}" for n, ts in offenders.items() for t in ts))


def _func_body_in(js: str, fn: str):
    """Brace-matched body of `function fn(...)` in a JS blob, or None if absent."""
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(fn) + r'\s*\(', js)
    if not m:
        return None
    i = js.index('{', m.start()); depth = 0
    for j in range(i, len(js)):
        if js[j] == '{':
            depth += 1
        elif js[j] == '}':
            depth -= 1
            if depth == 0:
                return js[i:j + 1]
    return None


def _norm_js(s: str) -> str:
    """Normalise a JS body for cross-surface comparison: decode \\uXXXX escapes to their character (so a
    literal '…' and '\\u2026' compare equal) and collapse whitespace."""
    s = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), s)
    return re.sub(r'\s+', ' ', s).strip()


def test_duplicated_system_helpers_stay_in_sync_across_phone_surfaces():
    """The two phone surfaces (launchpad + phone cockpit) each carry their OWN copy of the System-tab
    manage-cluster helpers. Every 'fixed on one surface, still broken on the other' bug (stale nonce,
    duplicate cards, nick-wipe) is a copy that drifted. Until these are extracted to a single shared
    block, this guard forces the copies that are MEANT to be identical to stay identical (unicode-escape
    and whitespace normalised), so a one-sided fix fails the gate instead of shipping a one-surface bug."""
    import mongoose.cockpit as ck
    launch = _inline_assets(ck.device_launchpad_html())
    phone = _inline_assets(ck.phone_cockpit_html())
    # Helpers that must be byte-identical across both surfaces (renderSystemManagePanels excluded: it
    # legitimately differs because loadPhoneSnapshots exists only on the phone-cockpit surface).
    shared = ["suFetch", "suLoadStages", "dumpCreate", "dumpLoadList", "suggLoad", "doDeviceUpdate"]
    drift = []
    for fn in shared:
        a, b = _func_body_in(launch, fn), _func_body_in(phone, fn)
        if a is None or b is None:
            continue
        if _norm_js(a) != _norm_js(b):
            drift.append(fn)
    assert not drift, (
        "System-manage helpers have DRIFTED between the two phone surfaces (a one-sided fix = a bug on "
        "one surface only; sync both copies or extract to a shared block): " + ", ".join(drift))
