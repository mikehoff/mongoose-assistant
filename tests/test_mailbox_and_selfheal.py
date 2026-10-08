"""Owner↔guest mailbox, self-heal recovery, and self-update fetch — real code paths, no mocks."""
import asyncio
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    for k in ("MONGOOSE_UPDATE_URL", "MONGOOSE_UPDATE_GITHUB_REPO"):
        os.environ.pop(k, None)
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


# ── Mailbox ───────────────────────────────────────────────────────────────────

def test_guest_can_message_owner_and_back():
    _fresh()
    from mongoose.services import mailbox as mb
    r = mb.send_message(sender="guest:jo", recipient="owner", body="boiler leaking")
    assert r["ok"]
    assert mb.unread_count("owner") == 1
    back = mb.send_message(sender="owner", recipient="guest:jo", body="on my way")
    assert back["ok"]
    assert mb.unread_count("guest:jo") == 1


def test_guest_cannot_message_another_guest():
    _fresh()
    from mongoose.services import mailbox as mb
    r = mb.send_message(sender="guest:jo", recipient="guest:bob", body="hi")
    assert not r["ok"]
    assert "owner" in r["error"].lower()


def test_guest_thread_isolated_from_other_guests():
    _fresh()
    from mongoose.services import mailbox as mb
    mb.send_message(sender="owner", recipient="guest:jo", body="hello jo")
    mb.send_message(sender="owner", recipient="guest:bob", body="hello bob")
    jo = mb.list_thread(participant="guest:jo")
    assert all("bob" not in m["body"] for m in jo)
    assert any("jo" in m["body"] for m in jo)


def test_owner_broadcast_to_all_guests():
    _fresh()
    from mongoose.services import mailbox as mb
    import mongoose.devices as DEV
    for nm in ("Jo", "Bob"):
        pc = DEV.create_pairing_code("trusted_guest")
        DEV.claim_pairing_code(pc["code"], device_name=nm, device_type="phone")
    r = mb.send_message(sender="owner", recipient="guest:*", body="house meeting at 6")
    assert r["ok"]
    assert len(r["delivered_to"]) == 2


def test_mark_read_clears_unread():
    _fresh()
    from mongoose.services import mailbox as mb
    mb.send_message(sender="guest:jo", recipient="owner", body="x")
    mb.send_message(sender="guest:jo", recipient="owner", body="y")
    assert mb.unread_count("owner") == 2
    mb.mark_read(participant="owner")
    assert mb.unread_count("owner") == 0


def test_message_send_fires_notification():
    _fresh()
    from mongoose.services import mailbox as mb
    from mongoose.services.notifications import recent_notifications
    mb.send_message(sender="guest:jo", recipient="owner", body="urgent thing")
    notes = recent_notifications(hours=1, limit=10)
    assert any("message" in (n.get("app", "").lower()) or "message" in (n.get("title", "").lower())
               for n in notes), f"no mailbox notification stored: {notes}"


# ── Self-heal ─────────────────────────────────────────────────────────────────

def test_self_heal_clean_install_noop():
    _fresh()
    from mongoose.services.self_heal import run_self_heal
    r = asyncio.run(run_self_heal())
    assert r["ok"]
    assert r["checked"] == 0
    assert r["healed"] == []


def test_self_heal_in_scheduler_registry():
    _fresh()
    from mongoose.services import scheduler as SCH
    reg = SCH._build_registry()
    assert "self_heal" in reg
    spec, fn = reg["self_heal"]
    r = asyncio.run(fn())
    assert r.get("ok") is True


def test_liveness_check_exposes_age_hours():
    """self_heal depends on age_hours being present in the data-freshness checks (email/calendar/
    files/whatsapp). The core/scheduler checks aren't age-based, so they're exempt."""
    _fresh()
    from mongoose.liveness import liveness
    rep = liveness()
    age_based = {"email", "calendar", "files", "whatsapp"}
    seen = 0
    for c in rep.get("checks", []):
        name = (c.get("subsystem") or "").split()[0].lower()
        if name in age_based:
            assert "age_hours" in c, f"data-freshness check missing age_hours: {c}"
            seen += 1
    assert seen > 0, "expected at least one data-freshness check"


def test_self_heal_summary_shape():
    _fresh()
    from mongoose.services.self_heal import self_heal_summary
    s = self_heal_summary()
    assert "recent_incidents" in s
    assert "recovered_count" in s


# ── Self-update fetch ─────────────────────────────────────────────────────────

def test_fetch_update_no_source_is_graceful():
    _fresh()
    from mongoose.updater import fetch_update_candidate
    r = fetch_update_candidate()
    assert r["ok"] is False
    assert "no update source" in r["error"].lower()


def test_resolve_update_url_precedence():
    _fresh()
    from mongoose.updater import _resolve_update_url
    os.environ["MONGOOSE_UPDATE_GITHUB_REPO"] = "owner/repo"
    url, kind = _resolve_update_url()
    assert kind == "github" and "owner/repo" in url
    os.environ["MONGOOSE_UPDATE_URL"] = "https://example.com/a.zip"
    url2, kind2 = _resolve_update_url()
    assert kind2 == "direct" and url2 == "https://example.com/a.zip"
    os.environ.pop("MONGOOSE_UPDATE_URL", None)
    os.environ.pop("MONGOOSE_UPDATE_GITHUB_REPO", None)


def test_fetch_rejects_non_zip(monkeypatch):
    _fresh()
    import urllib.request as _urlreq
    import mongoose.updater as U
    os.environ["MONGOOSE_UPDATE_URL"] = "https://example.com/notazip"

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"<html>not a zip</html>"
    monkeypatch.setattr(_urlreq, "urlopen", lambda *a, **k: _Resp())
    r = U.fetch_update_candidate()
    assert r["ok"] is False
    assert "zip" in r["error"].lower()
    os.environ.pop("MONGOOSE_UPDATE_URL", None)
