"""Release gate: response-shape contracts between page JavaScript and the endpoints that feed it.

The bug this exists for: `/welcome` read `s.steps` off the `/cockpit/onboarding` response and called
`s.steps.findIndex(...)`. Over LAN, for an unauthenticated client, that endpoint deliberately returns a
COARSE privacy-safe object with NO `steps` key, so the call threw and the page died silently. This was
NOT a missing function (the JS-validity gate would not catch it); it was a missing FIELD in one of the
two shapes the endpoint can return.

Crucially, the standard test client cannot even reach the failing path: Starlette's TestClient reports
its client host as 'testclient', which the app treats as loopback, so a naive request always gets the
FULL object and never the coarse LAN one. That is exactly why the bug escaped the suite. This gate uses
a tiny ASGI shim to force a non-loopback client host, so the LAN-coarse branch is actually exercised.

What the gate does, per page that renders state-driven JS:
  1. Drive the page's state endpoint in two modes: loopback owner, and LAN-unauthenticated.
  2. Compute the set of top-level fields the page's JS reads off that state object.
  3. For every field the JS reads, assert it is EITHER present in BOTH response shapes, OR guarded in the
     JS (Array.isArray / typeof / `&&` short-circuit / optional chaining / `if(x)` truthiness) so that
     its absence cannot throw.
A field that is read unguarded but can be absent in the LAN shape is a `/welcome`-class latent crash and
fails this gate.

SCOPE AND LIMITATIONS (deliberate, stated honestly):
  * This checks the explicitly-mapped page->state-endpoint contracts in CONTRACTS below, not every one of
    the ~574 `var.field` accesses across all page JS. A blanket sweep would be too noisy to trust (it
    cannot tell which response a given `d.x` belongs to). The mapped contracts are the state-driven pages
    where a shape mismatch silently kills the page, which is the demonstrated risk.
  * `_is_guarded` is a generous regex heuristic. A false "guarded" verdict is the safe direction: it only
    means this gate will not flag that particular field, not that it introduces a failure. The aim is to
    catch the clearly-unguarded `.findIndex()`/`.map()`-on-absent crash, which it does (proven by removing
    the /welcome guard and watching this gate go red).
  * It exercises two access modes (loopback owner, LAN-unauthenticated). It does not enumerate every auth
    permutation, and it does not prove the page renders correctly in a real browser, only that the JS
    cannot throw on a field that is missing from a shape it actually receives.
"""
from __future__ import annotations

import os
import re
import tempfile

import pytest

# ---------------------------------------------------------------------------------------------------
# ASGI shim: force a specific client host so we can drive the LAN (non-loopback) code path that the
# normal TestClient (host 'testclient', treated as loopback) can never reach.
# ---------------------------------------------------------------------------------------------------
class _ClientHostShim:
    def __init__(self, app, host: str):
        self.app = app
        self.host = host

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            scope = dict(scope)
            scope["client"] = (self.host, 54321)
        await self.app(scope, receive, send)


def _fresh_app_env():
    """A token MUST be configured for the coarse LAN branch to engage (tokenless first-run returns full
    detail by design). Set one, plus LAN host settings, in a throwaway data dir."""
    os.environ["MONGOOSE_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MONGOOSE_API_TOKEN"] = "contracttoken_test"
    os.environ["MONGOOSE_ALLOW_REMOTE"] = "true"
    os.environ["MONGOOSE_HOST"] = "0.0.0.0"


# ---------------------------------------------------------------------------------------------------
# The page->state-endpoint contracts we check. Kept explicit (not regex-guessed) so the gate is
# trustworthy: each entry says "this page renders this endpoint's object and reads these fields off it".
# `state_fields_re` extracts the fields the page reads off the state variable name used in its renderer.
# ---------------------------------------------------------------------------------------------------
CONTRACTS = [
    # page renderer,         state endpoint,            JS state var(s) whose `.field` reads matter
    ("welcome_html",        "/cockpit/onboarding",      ("s",)),
    ("install_html",        "/install/progress",        ("p",)),
    # install's pollPaired() also reads from the coarse-split /cockpit/onboarding (it reads s.signals,
    # which must be guarded since the coarse LAN shape may omit it). Same endpoint that killed /welcome.
    ("install_html",        "/cockpit/onboarding",      ("s",)),
]


def _page_js(renderer_name: str) -> str:
    from mongoose import cockpit as C
    html = getattr(C, renderer_name)()
    return " ".join(re.findall(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.DOTALL))


def _consumer_region(js: str, endpoint: str, var: str) -> str | None:
    """Return the slice of JS from the point where `var` is assigned this endpoint's result up to a
    reasonable scope boundary, so we read fields off the RIGHT object. A page may reuse a short variable
    name (e.g. `s`) for both an endpoint response and an unrelated loop item; collecting `s.field` across
    the whole script would conflate them. We anchor on `var = await j("endpoint")` / `var = await
    api("endpoint")` / `var = await fetch("endpoint")` and take the following ~1200 chars (the consuming
    block) as the region whose `var.field` reads pertain to this response."""
    ep = re.escape(endpoint)
    m = re.search(rf"\b{re.escape(var)}\s*=\s*await\s+(?:j|api|fetch)\(\s*[\"']{ep}[\"']", js)
    if not m:
        return None
    start = m.start()
    return js[start:start + 1200]


def _fields_read_for(js: str, endpoint: str, varnames: tuple[str, ...]) -> tuple[set[str], str]:
    """Fields read off the endpoint's response object, scoped to the consuming region when we can find
    the assignment site; falls back to whole-script for the var if no explicit `var = await ...endpoint`
    site exists (e.g. the response flows through a render(state) parameter rather than a local var)."""
    fields: set[str] = set()
    scope_used = js
    for v in varnames:
        region = _consumer_region(js, endpoint, v)
        if region is not None:
            scope_used = region
            fields |= set(re.findall(rf"\b{re.escape(v)}\.([a-zA-Z_]\w*)", region))
        else:
            # no local assignment: the var is a renderer parameter (render(s) called with the response).
            # Use the whole script for that var, which is correct when the name is not reused.
            fields |= set(re.findall(rf"\b{re.escape(v)}\.([a-zA-Z_]\w*)", js))
    return fields, scope_used


def _fields_read(js: str, varnames: tuple[str, ...]) -> set[str]:
    fields: set[str] = set()
    for v in varnames:
        fields |= set(re.findall(rf"\b{re.escape(v)}\.([a-zA-Z_]\w*)", js))
    return fields


def _is_guarded(js: str, var: str, field: str) -> bool:
    """Heuristic, deliberately generous (a false 'guarded' is safe; we only want to catch the clearly
    unguarded `.findIndex()`-style crash). A field is considered guarded if anywhere in the JS it appears
    under Array.isArray, a typeof check, an optional chain, a truthiness `&&`/`if`, or a `(x.field||...)`
    default."""
    f = re.escape(field)
    v = re.escape(var)
    patterns = [
        rf"Array\.isArray\(\s*{v}\.{f}\s*\)",          # Array.isArray(s.steps)
        rf"typeof\s+{v}\.{f}",                          # typeof s.steps
        rf"{v}\.{f}\s*\?\.",                            # s.steps?.
        rf"{v}\?\.{f}",                                 # s?.steps
        rf"{v}\.{f}\s*&&",                              # s.steps && ...
        rf"&&\s*{v}\.{f}",                              # ... && s.steps
        rf"if\s*\(\s*{v}\.{f}\b",                       # if(s.steps)
        rf"if\s*\(\s*!\s*{v}\.{f}\b",                   # if(!s.steps)
        rf"if\s*\(\s*!\s*Array\.isArray\(\s*{v}\.{f}",  # if(!Array.isArray(s.steps))
        rf"\(\s*{v}\.{f}\s*\|\|",                       # (s.steps || [])
        rf"{v}\.{f}\s*\|\|",                            # s.steps || []
        rf"{v}\.{f}\s*===?\s*",                         # s.complete === true (comparison, not deref)
        rf"{v}\.{f}\s*!==?\s*",
    ]
    return any(re.search(p, js) for p in patterns)


def _drive(endpoint: str):
    """Return (loopback_json, lan_unauth_json). Reimports the app fresh against the token env so the
    coarse branch is live."""
    import importlib
    import mongoose.app as A
    importlib.reload(A)
    from fastapi.testclient import TestClient

    loop = TestClient(A.app)
    r_local = loop.get(endpoint)
    local_json = r_local.json() if r_local.headers.get("content-type", "").startswith("application/json") else {}

    lan = TestClient(_ClientHostShim(A.app, "192.168.1.50"))
    r_lan = lan.get(endpoint)  # no auth header -> coarse branch when a token is set
    lan_json = r_lan.json() if r_lan.headers.get("content-type", "").startswith("application/json") else {}
    return local_json, lan_json, r_local.status_code, r_lan.status_code


@pytest.mark.parametrize("renderer,endpoint,varnames", CONTRACTS)
def test_page_state_fields_present_or_guarded_in_both_shapes(renderer, endpoint, varnames):
    _fresh_app_env()
    js = _page_js(renderer)
    read, region = _fields_read_for(js, endpoint, varnames)
    assert read, f"{renderer}: found no `{varnames}.field` reads for {endpoint} , did the page change shape?"

    local_json, lan_json, sc_local, sc_lan = _drive(endpoint)
    assert sc_local == 200, f"{endpoint} loopback returned {sc_local}, expected 200"

    local_keys = set(local_json.keys()) if isinstance(local_json, dict) else set()
    lan_keys = set(lan_json.keys()) if isinstance(lan_json, dict) else set()

    # The risk: a field the JS reads is present in the loopback shape but ABSENT in the LAN shape, and
    # the JS dereferences it without a guard. That is the /welcome crash. Guard-detection runs against
    # the consuming region (so a guard local to the consuming function counts).
    var = varnames[0]
    latent = []
    for field in sorted(read):
        if field in local_keys and field in lan_keys:
            continue  # present in both shapes, safe
        if not _is_guarded(region, var, field):
            latent.append(field)

    assert not latent, (
        f"{renderer} reads field(s) off `{var}.` that can be ABSENT in the LAN/coarse shape of {endpoint} "
        f"and are dereferenced WITHOUT a guard (the /welcome s.steps crash class):\n"
        f"  unguarded+absent: {latent}\n"
        f"  loopback keys: {sorted(local_keys)}\n"
        f"  LAN keys:      {sorted(lan_keys)}\n"
        f"  Fix: guard each (Array.isArray / typeof / && / optional chaining / `|| default`) or have the "
        f"endpoint include the field in both shapes."
    )


def test_lan_coarse_branch_is_actually_reachable():
    """Guard against the gate silently testing nothing: prove the ASGI shim really does reach the coarse
    LAN branch (different keys from loopback) for /cockpit/onboarding. If this ever stops being true, the
    contract test above would pass vacuously, so fail loudly here instead."""
    _fresh_app_env()
    local_json, lan_json, sc_local, sc_lan = _drive("/cockpit/onboarding")
    assert sc_local == 200
    local_keys = set(local_json.keys()) if isinstance(local_json, dict) else set()
    lan_keys = set(lan_json.keys()) if isinstance(lan_json, dict) else set()
    # the whole point: loopback exposes 'steps', the coarse LAN object does not
    assert "steps" in local_keys, f"loopback onboarding lost its 'steps' field: {sorted(local_keys)}"
    assert "steps" not in lan_keys, (
        "the LAN-coarse branch was NOT exercised (it still has 'steps'), so the shim/host logic changed "
        f"and this gate would test nothing. LAN keys: {sorted(lan_keys)}"
    )
