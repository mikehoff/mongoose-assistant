"""The install/pairing flow defects from the field, locked down:
1. enable-phone restart must be DETACHED (inline `mongoose stop` killed the server it was
   restarting, and the relaunch line never ran).
2. The install page must never blind-reload onto a dead port; it polls /health.
3. Owner handoff links preserve the loopback origin the user is on (localhost and 127.0.0.1
   are different origins with separate cert acceptances and service workers).
4. The reachability warning is legible (QR box background reset) and states its CAUSE via
   the diagnosis fields the server now attaches.
5. serve never regenerates an existing, unexpired self-signed cert (trust stability)."""
from __future__ import annotations

import pathlib
import warnings

warnings.filterwarnings("ignore")

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_enable_phone_restart_is_detached():
    """CORRECTED: the detached-restart subprocess code used to be inlined directly in
    _enable_phone_and_schedule_restart; it moved to a SHARED helper
    (_schedule_detached_phone_restart) so the enable-phone button and the pairing-flow
    auto-repair path can never drift into two different restart mechanisms again. The old
    text-slice assertion checked an implementation detail (where the code lived) rather than
    the actual behavioural contract (detachment, start_new_session, the helper script)."""
    src = (ROOT / "src" / "mongoose" / "app.py").read_text()
    seg = src[src.index("def _schedule_detached_phone_restart"):
              src.index("def _repair_phone_reachability")]
    assert 'subprocess.run([sys.executable, "-m", "mongoose.cli", "stop"]' not in seg
    assert "start_new_session=True" in seg
    assert '"-c", helper' in seg
    # And the two callers (the explicit button, and the auto-repair path) both go through it.
    caller_seg = src[src.index("async def _enable_phone_and_schedule_restart"):
                     src.index('@app.post("/cockpit/enable-phone")')]
    assert "_schedule_detached_phone_restart()" in caller_seg


def test_install_page_polls_health_instead_of_blind_reload():
    from fastapi.testclient import TestClient
    import mongoose.app as A
    html = TestClient(A.app).get("/install").text
    assert "waitForServerThenReload" in html
    assert "setTimeout(()=>location.reload(), 11000)" not in html
    assert 'j("/health"' in html


def test_owner_links_preserve_loopback_origin():
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    for path in ("/install", "/welcome"):
        html = c.get(path).text
        assert 'const loop=(h==="localhost"||h==="127.0.0.1"' in html, path
        assert 'return location.protocol+"//"+(loop?h:"localhost")+":"+p;' in html, path


def test_reachability_warning_is_legible_and_explains_itself(monkeypatch):
    from fastapi.testclient import TestClient
    import mongoose.app as A
    monkeypatch.delenv("MONGOOSE_HOST", raising=False)
    monkeypatch.delenv("MONGOOSE_ALLOW_REMOTE", raising=False)
    c = TestClient(A.app)
    d = c.post("/install/pair-code").json()
    assert d.get("reachability_warning") == "not_lan_reachable"
    g = d.get("reachability_diagnosis") or {}
    for key in ("effective_host", "effective_allow_remote", "env_file_host",
                "env_file_allow_remote", "env_path"):
        assert key in g
    html = c.get("/install").text
    branch = html.split("reachability_warning")[1][:700]
    assert 'qrEl.style.background="transparent"' in branch
    assert "reachability_diagnosis" in html


def test_serve_keeps_existing_unexpired_cert():
    src = (ROOT / "src" / "mongoose" / "cli.py").read_text()
    seg = src[src.index("No usable cert configured"):src.index("Could not set up HTTPS automatically")]
    assert "files_exist and not expired" in seg
    assert "stability beats freshness" in seg
