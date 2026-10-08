"""Phone-pairing self-heal (external triage, second submission): blocking the QR was
necessary but not the actual fix: it stopped Mongoose lying about reachability but did
nothing to make the phone actually connect. When the LAN socket is unreachable, Mongoose now
tries to repair it (write the settings a phone needs, restart the stale listener) before
falling back to a plain block. Auto-repair only fires when the caller explicitly opts in
(auto_repair flag); both routes it lives behind are already owner-only, so this is a new
TRIGGER for existing, already-trusted capability (ensure_phone_access_defaults, the same
function the pre-existing Enable-phone button calls), not new capability or a new boundary.
The detached-restart mechanism is now shared between the button and the auto-repair path so
they can never drift into two different implementations again."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "p.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app), A


def _mock_repair_chain(monkeypatch, A):
    calls = {"settings_written": False, "restart_scheduled": False}
    def fake_ensure():
        calls["settings_written"] = True
        return {"MONGOOSE_HOST": "0.0.0.0"}
    def fake_schedule():
        calls["restart_scheduled"] = True
        return {"ok": True, "restarting": True, "detail": "repairing"}
    import mongoose.enable_phone as EP
    monkeypatch.setattr(EP, "ensure_phone_access_defaults", fake_ensure)
    monkeypatch.setattr(A, "_schedule_detached_phone_restart", fake_schedule)
    return calls


def test_install_pair_code_auto_repairs_only_when_flag_is_set(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    calls = _mock_repair_chain(monkeypatch, A)
    r = c.post("/install/pair-code?auto_repair=1")
    j = r.json()
    assert j.get("qr_blocked") is True
    assert j["auto_repair"]["restarting"] is True
    assert calls["settings_written"] and calls["restart_scheduled"]


def test_install_pair_code_never_repairs_without_the_flag(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    calls = _mock_repair_chain(monkeypatch, A)
    r = c.post("/install/pair-code")
    j = r.json()
    assert j.get("qr_blocked") is True
    assert "auto_repair" not in j
    assert not calls["settings_written"] and not calls["restart_scheduled"]


def test_cockpit_pair_code_auto_repairs_for_owner_profiles_only(monkeypatch, tmp_path):
    c, A = _setup(monkeypatch, tmp_path)
    calls = _mock_repair_chain(monkeypatch, A)
    H = {"X-Cockpit-Nonce": A._COCKPIT_NONCE}
    r = c.post("/cockpit/devices/pair-code", json={"profile": "primary_phone", "auto_repair": True}, headers=H)
    j = r.json()
    assert j.get("qr_blocked") is True and j["auto_repair"]["restarting"] is True
    assert calls["settings_written"] and calls["restart_scheduled"]


def test_trusted_guest_pairing_is_never_auto_repaired(monkeypatch, tmp_path):
    """A guest's reachability question is DDNS, not this Mac's LAN socket; auto-repair must
    never fire for the guest path even if the flag is somehow set."""
    c, A = _setup(monkeypatch, tmp_path, )
    calls = _mock_repair_chain(monkeypatch, A)
    monkeypatch.setenv("DDNS_PROVIDER", "duckdns")
    monkeypatch.setenv("DDNS_DUCKDNS_DOMAIN", "yourname")
    monkeypatch.setenv("DDNS_DUCKDNS_TOKEN", "t")
    H = {"X-Cockpit-Nonce": A._COCKPIT_NONCE}
    r = c.post("/cockpit/devices/pair-code", json={"profile": "trusted_guest", "auto_repair": True}, headers=H)
    j = r.json()
    assert "auto_repair" not in j
    assert not calls["settings_written"] and not calls["restart_scheduled"]


def test_repair_reports_reachable_directly_when_nothing_is_actually_wrong(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "r.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.db import init_db
    init_db()
    import mongoose.app as A
    monkeypatch.setattr(A, "_lan_reachable_now", lambda: True)
    res = A._repair_phone_reachability("test")
    assert res == {"ok": True, "needed": False, "reachable": True, "diagnosis": res["diagnosis"]}


def test_restart_helper_is_debounced_against_repeated_polls(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    import subprocess
    import mongoose.app as A
    monkeypatch.setattr(A, "_PHONE_REPAIR_LAST_STARTED", 0.0)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 1})())
    res1 = A._schedule_detached_phone_restart()
    res2 = A._schedule_detached_phone_restart()
    assert not res1.get("already_scheduled")
    assert res2.get("already_scheduled") is True


def test_enable_phone_button_and_auto_repair_share_the_same_restart_mechanism():
    """Both call sites must go through the exact same function (external triage point 6:
    the button and the auto-repair path must never drift into two implementations)."""
    import inspect
    import mongoose.app as A
    button_src = inspect.getsource(A._enable_phone_and_schedule_restart)
    repair_src = inspect.getsource(A._repair_phone_reachability)
    assert "_schedule_detached_phone_restart" in button_src
    assert "_schedule_detached_phone_restart" in repair_src


def test_all_three_rendered_pages_define_their_own_copy_of_the_poll_helper():
    """cockpit_html, WELCOME_HTML and INSTALL_HTML are three separate rendered documents with
    independent inline scripts, not a shared JS file; each must define the function it calls
    or the call is a runtime ReferenceError with nothing to catch it."""
    from mongoose.cockpit import cockpit_html, welcome_html, install_html
    for name, html in (("cockpit", cockpit_html()), ("welcome", welcome_html()), ("install", install_html())):
        assert html.count("async function waitForMongooseThenReload") == 1, name
        assert "waitForMongooseThenReload(" in html, name
