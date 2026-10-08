"""Fixes from the external 1.37.0 guest-on-phone audit, pinned: (1) the tokenless
/guest/invite page (the page itself strips the token; refresh must not 404), (2) terminal
invite states that say what to DO, (6) no error_detail to guest tokens, (8) warm
empty-message copy, (11) mongoose says in-room when its brain is down instead of silence,
(13) colliding lounge names never mark someone else's line as mine."""
from __future__ import annotations

import json
import warnings

warnings.filterwarnings("ignore")


def _setup(monkeypatch, tmp_path):
    for k, v in {"MONGOOSE_DB_PATH": str(tmp_path / "g.sqlite3"),
                 "MONGOOSE_DATA_DIR": str(tmp_path / "d"),
                 "MONGOOSE_ALLOW_REMOTE": "true", "MONGOOSE_HOST": "0.0.0.0",
                 "MONGOOSE_API_TOKEN": "t", "MONGOOSE_LOUNGE_STARTERS": "off",
                 "MONGOOSE_PUBLIC_URL": "https://example.duckdns.org"}.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app)


def _guest(c, name, label):
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    inv = create_guest_invite(owner_label=label)
    tok = inv["invite_url"].rstrip("/").split("/")[-1]
    cl = claim_guest_invite(tok, {"mongoose_name": name, "public_name": name, "fun_fact": "x"})
    return {"X-Mongoose-Device-Token": cl["device_token"]}


def test_tokenless_invite_page_is_friendly_not_404(monkeypatch, tmp_path):
    c = _setup(monkeypatch, tmp_path)
    r = c.get("/guest/invite")
    assert r.status_code == 200 and "hid the invite link" in r.text


def test_terminal_invite_states_say_what_to_do(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    from mongoose.guest_invites import invite_status
    assert "ask them for a new one" in invite_status("nonsense")["message"]


def test_guest_chat_and_lounge_never_leak_backend_detail(monkeypatch, tmp_path):
    c = _setup(monkeypatch, tmp_path)
    H = _guest(c, "Lee", "rin")
    j = c.post("/device/chat", headers=H, json={"message": "hello"}).json()
    assert j.get("brain_down") and "error_detail" not in j
    e = c.post("/device/chat", headers=H, json={"message": " "}).json()
    assert "all ears" in e["error"]
    s = c.post("/device/irc/send", headers=H, json={"text": "mongoose you there?"}).json()
    blob = json.dumps(s)
    assert "Traceback" not in blob and "Ollama" not in blob and "Connection" not in blob
    # model down must be VISIBLE in the room (a mongoose line lands), wording-agnostic
    hist = c.get("/device/irc/history", headers=H).json()
    assert any(m.get("mongoose") for m in hist["messages"])


def test_colliding_lounge_names_get_exact_per_device_attribution(monkeypatch, tmp_path):
    """Upgraded contract (external group-surpriser patch): two guests who both choose the
    lounge name Alex each see exactly THEIR OWN lines as mine, via the lounge_message_meta
    device-id side table. Supersedes the interim never-claim-mine behaviour."""
    c = _setup(monkeypatch, tmp_path)
    H1 = _guest(c, "Alex", "a1")
    H2 = _guest(c, "Alex", "a2")
    c.post("/device/irc/send", headers=H1, json={"text": "first alex speaking"})
    c.post("/device/irc/send", headers=H2, json={"text": "second alex speaking"})
    h1 = c.get("/device/irc/history", headers=H1).json()["messages"]
    h2 = c.get("/device/irc/history", headers=H2).json()["messages"]
    mine1 = [m["text"] for m in h1 if m.get("mine")]
    mine2 = [m["text"] for m in h2 if m.get("mine")]
    assert any("first alex" in x for x in mine1) and not any("second alex" in x for x in mine1)
    assert any("second alex" in x for x in mine2) and not any("first alex" in x for x in mine2)


def test_lounge_poke_sends_push_and_posts_room_line(monkeypatch, tmp_path):
    """'poke sam' in the lounge fuzzy-matches Samster, sends a friendly push through
    send_to_phone, and posts a visible confirmation line; unknown names get a gentle miss.
    Two DIFFERENT senders are used for the two assertions: a real rate limit (added after
    this test was first written) throttles a second poke from the SAME sender within its
    cooldown window, which is exercised on its own in test_hardening_pass.py."""
    c = _setup(monkeypatch, tmp_path)
    import mongoose.services.notifications as N
    sent = {}
    async def fake(title, body="", **kw):
        sent.update(title=title, body=body)
        # simulate one reachable subscription so the poke reports delivery
        return {"ok": True, "sent": 1}
    monkeypatch.setattr(N, "send_to_phone", fake)
    H1 = _guest(c, "Kasia", "k")
    H2 = _guest(c, "Zed", "z")
    _guest(c, "Samster", "g")
    r = c.post("/device/irc/send", headers=H1, json={"text": "poke sam"}).json()
    assert "poked Samster" in r["reply"] and "poked you" in sent["body"]
    miss = c.post("/device/irc/send", headers=H2, json={"text": "poke zzzz"}).json()
    assert "couldn't find a guest" in miss["reply"]


def test_doctor_reports_notifications_end_to_end(monkeypatch, tmp_path):
    """Zero phone subscriptions must be VISIBLE, because zero subscriptions is exactly what
    'I never got a single notification' looks like. Chain-aware since the setup_state
    consolidation: if phone-at-home itself isn't reachable, THAT is the blocker, correctly
    reported before subscription count even matters (matches the guest_audit A5 finding's
    original intent: never let 'it never worked' hide behind a message that assumes a step
    that hasn't happened yet)."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "d.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from mongoose.doctor import run_doctor
    n = run_doctor()["checks"]["notifications"]
    assert n["ok"] is False
    assert "blocked by phone setup" in n["detail"]

    # With phone-at-home reachable, the ORIGINAL subscription-visibility fact surfaces.
    import mongoose.app as A
    monkeypatch.setattr(A, "_lan_reachable_now", lambda: True)
    n2 = run_doctor()["checks"]["notifications"]
    assert n2["ok"] is False and "0 web-push subscriptions" in n2["detail"]
    assert "allow notifications" in n2["hint"]


def test_launchpad_nudges_notification_permission():
    from mongoose.cockpit import device_launchpad_html
    h = device_launchpad_html()
    assert "Turn on notifications" in h
    assert "getSubscription" in h  # silent resubscribe path
    assert "enablePush" in h       # wired to the REAL subscribe flow, not a bare permission ask
