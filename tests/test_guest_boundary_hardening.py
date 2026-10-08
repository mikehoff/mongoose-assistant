"""Guest boundary hardening: a trusted_guest token is default-DENY on the device surface (owner-only
routes 403 at the gate), the few guest-reachable data routes are guest-scoped (no owner state), the
invite claim is atomic and gated on the real server version, and invite tokens are kept out of logs
and the address bar. These cover the leaks found in review where a guest could read owner summaries /
readiness or write into owner stores.
"""
from __future__ import annotations


from fastapi.testclient import TestClient


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MONGOOSE_ALLOW_REMOTE", raising=False)
    monkeypatch.delenv("MONGOOSE_API_TOKEN", raising=False)
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()


def _remote_client(monkeypatch):
    import mongoose.app as A
    monkeypatch.setattr(A, "_LOOPBACK_HOSTS", {"127.0.0.1", "::1", "localhost"})
    return TestClient(A.app, raise_server_exceptions=False)


def _guest_token():
    from mongoose import devices as D
    code = D.create_pairing_code(profile="trusted_guest")["code"]
    return D.claim_pairing_code(code, "Guest Phone", "phone")["device_token"]


# ── the gate: owner-only device routes are closed to guests (default-deny) ──────────────────────────
OWNER_ONLY = [
    ("POST", "/device/upload"),
    ("POST", "/device/share"),
    ("POST", "/device/event"),
    ("GET", "/device/readiness"),
    ("GET", "/device/assets"),
    ("GET", "/device/assets/some-id"),
    ("GET", "/device/notifications"),
    ("POST", "/device/notifications/ingest"),
    ("GET", "/device/remote-access/router"),
    ("POST", "/device/update/upload"),
]

ALLOWLISTED = [
    ("GET", "/device/whoami"),
    ("GET", "/device/summary"),
    ("GET", "/device/chat/history"),
    ("GET", "/device/tutorial"),
    ("GET", "/device/guest/people"),
]


def test_guest_blocked_from_owner_only_device_routes(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    tok = _guest_token()
    c = _remote_client(monkeypatch)
    h = {"X-Mongoose-Device-Token": tok}
    for method, path in OWNER_ONLY:
        r = c.request(method, path, headers=h, json={})
        assert r.status_code == 403, f"{method} {path} should be 403 for a guest, got {r.status_code}"


def test_guest_allowed_on_allowlisted_device_routes(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    tok = _guest_token()
    c = _remote_client(monkeypatch)
    h = {"X-Mongoose-Device-Token": tok}
    for method, path in ALLOWLISTED:
        r = c.request(method, path, headers=h)
        assert r.status_code != 403, f"{method} {path} should reach its handler for a guest"


# ── guest-reachable data routes are guest-scoped (no owner state) ───────────────────────────────────
def test_guest_summary_carries_no_owner_state(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    tok = _guest_token()
    c = _remote_client(monkeypatch)
    r = c.get("/device/summary", headers={"X-Mongoose-Device-Token": tok})
    assert r.status_code == 200
    body = r.json()
    assert body.get("guest") is True
    assert body["what_changed"]["changed"] is False and body["what_changed"]["count"] == 0
    assert body["suggestions"] == []
    assert body["approvals"]["pending"] == 0
    assert body["daily"]["what_needs_you"] == [] and body["daily"]["counts"] == {}


def test_guest_summary_never_computes_owner_picture(tmp_path, monkeypatch):
    """The guest branch must return before the owner daily-picture / suggester / drafts run."""
    _fresh(tmp_path, monkeypatch)
    tok = _guest_token()
    import mongoose.services.daily_picture as DP
    import mongoose.services.surpriser as SUR

    async def _boom_dp():
        raise AssertionError("guest summary must not compute the owner daily operating picture")

    async def _boom_sur():
        raise AssertionError("guest summary must not call the owner suggester")

    monkeypatch.setattr(DP, "daily_operating_picture", _boom_dp)
    monkeypatch.setattr(SUR, "candidate_suggestions", _boom_sur)
    c = _remote_client(monkeypatch)
    r = c.get("/device/summary", headers={"X-Mongoose-Device-Token": tok})
    assert r.status_code == 200 and r.json().get("guest") is True


def test_guest_tutorial_forces_non_private(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    tok = _guest_token()
    import mongoose.services.tutorial as T
    seen = {}
    monkeypatch.setattr(T, "intro_for_phone", lambda include_private_state=True: seen.update(intro=include_private_state) or {"ok": True})
    monkeypatch.setattr(T, "intro_speech", lambda include_private_state=True: seen.update(speech=include_private_state) or "hi")
    c = _remote_client(monkeypatch)
    r = c.get("/device/tutorial", headers={"X-Mongoose-Device-Token": tok})
    assert r.status_code == 200
    assert seen.get("intro") is False and seen.get("speech") is False


def test_guest_tutorial_cool_forbids_private_data(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    tok = _guest_token()
    import mongoose.services.tutorial as T
    seen = {}

    async def _cap(allow_private_data=True):
        seen["allow"] = allow_private_data
        return {"ok": True}

    monkeypatch.setattr(T, "do_something_cool", _cap)
    c = _remote_client(monkeypatch)
    r = c.post("/device/tutorial/cool", headers={"X-Mongoose-Device-Token": tok})
    assert r.status_code == 200
    assert seen.get("allow") is False


# ── invite claim: atomic + server-version gated ─────────────────────────────────────────────────────
def _make_invite(min_version="1.1"):
    from mongoose.guest_invites import create_guest_invite
    res = create_guest_invite(owner_label="Jo", min_version=min_version,
                              base_url="https://yourname.duckdns.org:8765")
    url = res["invite_url"]
    return url.rsplit("/", 1)[-1]


def _claim_payload():
    return {"mongoose_name": "Jo", "public_name": "Jo", "fun_fact": "I test races."}


def test_invite_claims_once_then_already_claimed(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.guest_invites import claim_guest_invite
    token = _make_invite()
    first = claim_guest_invite(token, _claim_payload())
    assert first.get("ok") is True
    second = claim_guest_invite(token, _claim_payload())
    assert second.get("ok") is False and second.get("error") == "already_claimed"
    # exactly one guest device exists for this invite
    from mongoose.db import connect
    with connect() as con:
        n = con.execute("SELECT COUNT(*) FROM paired_devices WHERE profile='trusted_guest'").fetchone()[0]
    assert n == 1


def test_invite_version_gate_uses_server_version(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.guest_invites import claim_guest_invite
    token = _make_invite(min_version="9.9")
    # even with a spoofed high client_version, the gate is the real server version
    res = claim_guest_invite(token, {**_claim_payload(), "client_version": "99.9"})
    assert res.get("ok") is False and res.get("error") == "server_version_too_old"


# ── token never leaks into logs or the address bar ──────────────────────────────────────────────────
def test_safe_path_for_log_redacts_invite_token():
    import mongoose.app as A
    assert A._safe_path_for_log("/guest/invite/SECRETTOKEN") == "/guest/invite/[redacted]"
    assert A._safe_path_for_log("/guest/invite/SECRETTOKEN/claim") == "/guest/invite/[redacted]/claim"
    assert A._safe_path_for_log("/device/summary") == "/device/summary"  # untouched


def test_invite_page_sets_no_referrer_and_no_store(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    token = _make_invite()
    from mongoose.app import app
    c = TestClient(app)
    r = c.get(f"/guest/invite/{token}")
    assert r.status_code == 200
    assert r.headers.get("referrer-policy") == "no-referrer"
    assert "no-store" in (r.headers.get("cache-control") or "")


# ── guest chat never reaches owner deterministic handlers (readiness/RTM/alarms/delight/etc.) ────────
_OWNER_SOURCES = {"readiness", "sync_checkpoint", "rtm_knowledge", "rtm_build", "rtm_operational",
                  "time_alerts", "delight", "tutorial", "unstuck", "body_state", "body_literacy"}

_GUEST_TRIGGER_PROMPTS = [
    "are you ready?",
    "what's overdue on the RTM accounts?",
    "remind me to bring snacks tomorrow at 9am",
    "tell me a joke",
    "do something cool",
    "what can you do?",
    "i feel stuck",
    "log a swim",
]


def test_guest_chat_skips_every_owner_deterministic_handler(monkeypatch):
    import asyncio
    import mongoose.llm as L

    async def _canned(model, messages, settings):
        return "guest-safe reply"

    monkeypatch.setattr(L, "_chat_call", _canned)
    # belt-and-braces: a guest must never reach the owner alarm writer
    import mongoose.services.time_alerts as TA
    monkeypatch.setattr(TA, "set_from_text",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("guest reached owner alarms")))

    for p in _GUEST_TRIGGER_PROMPTS:
        out = asyncio.run(L.chat_with_memory(p, conversation_id="guest:devX", guest_mode=True))
        assert out.get("source") not in _OWNER_SOURCES, f"guest prompt {p!r} hit owner source {out.get('source')}"
    # a neutral prompt still reaches the guest-safe chat path (mock proves the path is live)
    out = asyncio.run(L.chat_with_memory("hello there", conversation_id="guest:devX", guest_mode=True))
    assert out["reply"] == "guest-safe reply"


def test_owner_chat_still_uses_deterministic_handlers(monkeypatch):
    """The guard is guest-only: an owner readiness question still routes to the readiness layer."""
    import asyncio
    import mongoose.llm as L
    import mongoose.services.readiness_presenter as RP
    monkeypatch.setattr(RP, "readiness_for_voice", lambda: {"speech": "All systems ready."})
    out = asyncio.run(L.chat_with_memory("are you ready?", conversation_id="owner:test", guest_mode=False))
    assert out.get("source") == "readiness"


def test_guest_claim_sets_display_name_so_launchpad_does_not_reask(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.guest_invites import claim_guest_invite
    token = _make_invite()
    res = claim_guest_invite(token, {"mongoose_name": "Anna", "public_name": "Anna from Flat 4",
                                     "fun_fact": "I bake bread."})
    assert res.get("ok") is True
    from mongoose.db import connect
    with connect() as con:
        row = con.execute("SELECT display_name FROM paired_devices WHERE profile='trusted_guest'").fetchone()
    assert row["display_name"] == "Anna"  # maybeAskName() sees this and won't ask again
