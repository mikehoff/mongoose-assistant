"""Self-wiring permissions: authorizing a connector activates its capabilities immediately
(first sync) instead of waiting for the next scheduled sync. Connector libs aren't present in
tests, so syncs defer gracefully; the wiring orchestration is what's verified."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def test_unknown_provider_is_graceful():
    _fresh()
    from mongoose.services.self_wiring import wire_after_authorize
    r = wire_after_authorize("nope")
    assert r["wired"] == []
    assert "note" in r


def test_google_attempts_email_and_calendar():
    _fresh()
    from mongoose.services.self_wiring import wire_after_authorize
    r = wire_after_authorize("google")
    caps = {x["capability"] for x in r["results"]}
    assert caps == {"email", "calendar"}
    # No creds in test -> each is handled (has detail), nothing raised
    assert all("detail" in x for x in r["results"])


def test_microsoft_dedupes_calendar():
    _fresh()
    from mongoose.services.self_wiring import wire_after_authorize
    r = wire_after_authorize("microsoft")
    caps = [x["capability"] for x in r["results"]]
    assert caps.count("calendar") == 1
    assert set(caps) == {"calendar", "tasks", "email"}


def test_wiring_records_audit():
    _fresh()
    from mongoose.services.self_wiring import wire_after_authorize
    wire_after_authorize("google")
    from mongoose.db import connect
    with connect() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM audit_log WHERE module='self_wiring'").fetchone()
    assert row[0] >= 1


def test_wire_succeeds_when_a_sync_returns_data(monkeypatch):
    """With a capability sync that returns data, that capability is reported as wired."""
    _fresh()
    import mongoose.services.self_wiring as SW
    monkeypatch.setattr(SW, "_first_sync",
                        lambda cap, provider: {"capability": cap, "ok": cap == "email", "detail": "5 indexed"})
    r = SW.wire_after_authorize("google")
    assert "email" in r["wired"]
    assert r["any_data"] is True


def test_no_false_success_without_credentials():
    """The real-bug guard: without connector credentials, nothing falsely reports as wired, and
    no cross-provider fallback fires (a Google authorize must not sync Microsoft)."""
    _fresh()
    from mongoose.services.self_wiring import wire_after_authorize
    r = wire_after_authorize("google")
    assert r["wired"] == []
    for x in r["results"]:
        assert x["ok"] is False
        assert "No module named" not in x["detail"]  # import paths must resolve
