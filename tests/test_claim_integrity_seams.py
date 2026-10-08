"""Seam tests reconciled from the parallel v0.52.5 claim-integrity pass. The headline one is the
reduced-motion trap: the chat terminal CSS must not sit inside @media(prefers-reduced-motion), or
most users never see the lime-on-black UI (the bug that was reported repeatedly). The rest re-assert
first-run trust seams that both passes converged on."""
from __future__ import annotations

import os
import tempfile

from fastapi.testclient import TestClient


def _fresh_db():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    from mongoose.db import init_db
    init_db()
    return d


def test_install_page_is_never_cacheable():
    _fresh_db()
    from mongoose.app import app
    r = TestClient(app).get("/install")
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", "")


def test_chat_css_not_trapped_inside_reduced_motion_block():
    """THE regression test for the real chat-UI bug: the chat terminal CSS must not be inside any
    @media(prefers-reduced-motion) block (or most users never see the lime terminal). This asserts
    the invariant directly, so it holds whether the fix removed the stray media opener or closed it
    before the chat rules."""
    import re
    from mongoose.cockpit import cockpit_html
    html = cockpit_html()
    style = next(s for s in re.findall(r"<style>(.*?)</style>", html, re.S) if ".irc-win{" in s)
    irc = style.index(".irc-win{")
    # walk every prefers-reduced-motion block and confirm .irc-win is not within its braces
    for m in re.finditer(r"@media\s*\(prefers-reduced-motion[^{]*\{", style):
        depth = 0
        i = m.end() - 1
        end = None
        while i < len(style):
            if style[i] == "{":
                depth += 1
            elif style[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
            i += 1
        assert not (end and m.start() < irc < end), \
            "chat .irc-win CSS is trapped inside a prefers-reduced-motion block"
    # and the lime phosphor colour is actually present at top level
    assert "#39ff14" in style


def test_selfcheck_confirms_chat_lime_css_active():
    _fresh_db()
    from mongoose.app import app
    data = TestClient(app).get("/cockpit/selfcheck").json()
    assert data["chat_tab_present"] is True
    assert data["chat_lime_css_present"] is True
    assert data["chat_css_trapped_in_reduced_motion"] is False
    assert data["auto_reload_excludes_chat"] is True


def test_ha_local_name_error_is_actionable():
    _fresh_db()
    from mongoose.setup_wizard import verify_home_assistant
    ok, msg = verify_home_assistant({"HA_URL": "http://homeassistant.local:8123", "HA_TOKEN": "x"})
    assert ok is False
    assert "IP address" in msg
    assert "homeassistant.local" in msg


def test_files_and_scheduler_not_hardcoded_as_stale_on_fresh_install():
    _fresh_db()
    os.environ.pop("MONGOOSE_INDEX_ROOTS", None)
    from mongoose.liveness import liveness
    checks = {c["subsystem"]: c for c in liveness()["checks"]}
    assert checks["files"]["state"] == "not_configured"
    assert checks["scheduler"]["state"] == "not_configured"
