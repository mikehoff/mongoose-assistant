"""A deep-linked phone cockpit screen must load ONCE, not flicker 2 or 3 times.

Real bug: boot() calls openHashView() before the whoami round-trip, then boot().then(openHashView)
calls it again. The second call re-ran go(view), whose loader wiped the screen back to "Loading" and
repainted, so /device/cockpit#view=rtm (and every other #view= screen) visibly refreshed on open.
This runs the served openHashView in node against a stub DOM and counts go() calls.
"""
import json
import re
import shutil
import subprocess

import pytest

from mongoose import cockpit as C


def _fn_source(name: str) -> str:
    src = C.PHONE_COCKPIT_HTML
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    while True:
        ch = src[i]
        depth += ch == "{"
        depth -= ch == "}"
        i += 1
        if depth == 0:
            return src[start:i]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_open_hash_view_twice_loads_the_screen_once():
    harness = _fn_source("openHashView") + """
    var calls = [];
    var views = {rtm: {classList: {_on: false, contains: function(c){ return c === 'on' && this._on; }}}};
    var document = {getElementById: function(id){ return views[id.replace(/^v-/, '')] || null; }};
    var location = {hash: '#view=rtm'};
    function go(v){ calls.push(v); views[v].classList._on = true; }
    openHashView();   // boot(): before whoami
    openHashView();   // boot().then(openHashView): after whoami
    console.log(JSON.stringify(calls));
    """
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["rtm"], (
        "openHashView re-ran go() on the screen already showing, so the loader repainted and flickered")


def test_boot_still_honours_the_deep_link():
    src = C.PHONE_COCKPIT_HTML
    assert re.search(r"async function boot\(\)\{[^}]*openHashView\(\)", src), \
        "boot must still open the #view= screen before whoami so the home grid never flashes"
