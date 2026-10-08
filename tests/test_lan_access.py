"""Tests for LAN access: session-cookie login (so browsers/tablets work) and the
voice relay (browser records, server transcribes, owner replies). Auth-sensitive, so
these assert the cookie can't be forged, login needs the real token, and the relay is
nonce-gated and degrades cleanly without Whisper."""
import time

from fastapi.testclient import TestClient

import mongoose.session_auth as SA
from mongoose.app import app

client = TestClient(app)


# --- session signing ---------------------------------------------------------
def test_session_roundtrip():
    v = SA.issue_session(remember=False)
    assert SA.verify_session(v)


def test_session_rejects_garbage_and_tamper():
    v = SA.issue_session()
    assert not SA.verify_session("nope")
    assert not SA.verify_session("")
    assert not SA.verify_session(None)
    # tamper the mac
    assert not SA.verify_session(v[:-1] + ("0" if v[-1] != "0" else "1"))


def test_session_rejects_expired():
    assert not SA.verify_session(f"{int(time.time()) - 5}.deadbeef")


def test_remember_gives_longer_ttl():
    assert SA.cookie_max_age(True) > SA.cookie_max_age(False)


def test_token_matches(monkeypatch):
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "sekret")
    import mongoose.settings as st
    monkeypatch.setattr(st, "_SETTINGS", None, raising=False)
    assert SA.token_matches("sekret")
    assert not SA.token_matches("wrong")
    assert not SA.token_matches("")


def test_token_matches_false_when_no_token(monkeypatch):
    monkeypatch.delenv("MONGOOSE_API_TOKEN", raising=False)
    import mongoose.settings as st
    monkeypatch.setattr(st, "_SETTINGS", None, raising=False)
    # with no configured token, nothing matches (login can't succeed unset)
    assert not SA.token_matches("anything")


# --- login flow --------------------------------------------------------------
def test_login_page_served():
    r = client.get("/login")
    assert r.status_code == 200 and "Mongoose" in r.text


def test_login_rejects_wrong_token():
    r = client.post("/login", json={"token": "definitely-wrong"})
    assert r.status_code == 401


def test_login_sets_cookie_with_valid_token(monkeypatch):
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "loginsecret")
    import mongoose.settings as st
    monkeypatch.setattr(st, "_SETTINGS", None, raising=False)
    r = client.post("/login", json={"token": "loginsecret", "remember": True})
    assert r.status_code == 200
    sc = r.headers.get("set-cookie", "")
    assert SA.SESSION_COOKIE in sc
    assert "httponly" in sc.lower() and "samesite=lax" in sc.lower()


def test_logout_clears_cookie():
    r = client.post("/logout")
    assert r.status_code == 200


# --- voice relay -------------------------------------------------------------
def test_voice_requires_nonce():
    assert client.post("/cockpit/voice", content=b"audio").status_code == 403


def test_voice_empty_body(monkeypatch):
    n = client.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = client.post("/cockpit/voice", content=b"", headers={"X-Cockpit-Nonce": n})
    assert r.json()["ok"] is False


def test_voice_skips_without_whisper(monkeypatch):
    import mongoose.voice_benchmark as VB
    monkeypatch.setattr(VB, "_whisper_transcribe", lambda clip: None)
    n = client.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = client.post("/cockpit/voice", content=b"fakeaudio", headers={"X-Cockpit-Nonce": n})
    body = r.json()
    assert body["ok"] is False and "transcription" in body["error"].lower()


def test_voice_relays_to_owner(monkeypatch):
    """With Whisper present, the relay transcribes and routes through the owner."""
    import mongoose.voice_benchmark as VB
    monkeypatch.setattr(VB, "_whisper_transcribe", lambda clip: (0.5, "what changed today"))
    import mongoose.llm as llm
    async def fake_chat(prompt, *, conversation_id, role="chat", mode=None, system=None):
        assert role == "chat"  # the OWNER speaks, not a worker
        return {"reply": "Two new emails and a WhatsApp from Sam.", "conversation_id": conversation_id}
    monkeypatch.setattr(llm, "chat_with_memory", fake_chat)
    n = client.get("/cockpit/nonce").json()["cockpit_nonce"]
    r = client.post("/cockpit/voice", content=b"audio", headers={"X-Cockpit-Nonce": n})
    body = r.json()
    assert body["ok"] is True
    assert body["transcript"] == "what changed today"
    assert "Sam" in body["reply"]


def test_voice_tab_in_cockpit():
    body = client.get("/ui").text
    assert "Voice" in body and "/cockpit/voice" in body


# --- audit v0.14.15 remediation ----------------------------------------------
def test_session_survives_restart_but_not_token_change(monkeypatch, tmp_path):
    """remember-device must survive a server restart (persistent salt), but a token
    change must still invalidate sessions."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "data" / "mongoose.sqlite3"))
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "tokA")
    import mongoose.settings as st
    monkeypatch.setattr(st, "_SETTINGS", None, raising=False)
    import importlib
    import mongoose.session_auth as S
    importlib.reload(S)
    v = S.issue_session(remember=True)
    assert S.verify_session(v)
    # simulate restart: new process salt, same on-disk salt
    importlib.reload(S)
    assert S.verify_session(v)
    # token change invalidates
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "tokB")
    monkeypatch.setattr(st, "_SETTINGS", None, raising=False)
    importlib.reload(S)
    assert not S.verify_session(v)


def test_login_throttle(monkeypatch):
    """After repeated failures the login endpoint throttles (429)."""
    import mongoose.app as A
    A._LOGIN_FAILS.clear()
    # no token configured -> every attempt fails
    monkeypatch.delenv("MONGOOSE_API_TOKEN", raising=False)
    import mongoose.settings as st
    monkeypatch.setattr(st, "_SETTINGS", None, raising=False)
    last = None
    for _ in range(7):
        last = client.post("/login", json={"token": "guess"})
    assert last.status_code == 429
    A._LOGIN_FAILS.clear()


def test_voice_rejects_oversize(monkeypatch):
    """The voice relay rejects an oversize upload."""
    n = client.get("/cockpit/nonce").json()["cockpit_nonce"]
    r2 = client.post("/cockpit/voice", content=b"x" * (26 * 1024 * 1024),
                     headers={"X-Cockpit-Nonce": n})
    assert r2.json()["ok"] is False and "large" in r2.json()["error"]


def test_error_paths_escaped_in_cockpit():
    """Cockpit error renders use esc() (no raw ${e} into innerHTML)."""
    from mongoose.cockpit import cockpit_html
    body = cockpit_html()
    # the innerHTML error templates now escape the error value
    assert "${esc(e)}" in body
    # and there is no remaining raw `(${e})` inside an innerHTML template literal
    assert "Mongoose (${e})" not in body


def test_csrf_guard_blocks_cookie_only_writes(monkeypatch):
    """The core fix: a cookie-authenticated browser write to ANY private route must
    also carry the cockpit nonce, header-token callers (phone/script) are exempt."""
    import asyncio
    import mongoose.app as A
    import mongoose.session_auth as S
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "tok")
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    import mongoose.settings as st
    monkeypatch.setattr(st, "_SETTINGS", None, raising=False)

    class U: path = "/whatsapp/draft"
    class C: host = "192.168.1.50"
    class Req:
        def __init__(self, method, cookies, headers):
            self.url = U(); self.client = C(); self.method = method
            self.cookies = cookies; self.headers = headers

    async def nxt(req):
        from types import SimpleNamespace
        return SimpleNamespace(status_code=200)

    cookie = S.issue_session()
    # cookie POST without nonce -> 403
    r1 = asyncio.run(A._guard_sensitive_routes(Req("POST", {S.SESSION_COOKIE: cookie}, {}), nxt))
    assert r1.status_code == 403
    # cookie POST with nonce -> through
    r2 = asyncio.run(A._guard_sensitive_routes(
        Req("POST", {S.SESSION_COOKIE: cookie}, {"x-cockpit-nonce": A._COCKPIT_NONCE}), nxt))
    assert r2.status_code == 200
    # cookie GET (read) -> through, no nonce needed
    r3 = asyncio.run(A._guard_sensitive_routes(Req("GET", {S.SESSION_COOKIE: cookie}, {}), nxt))
    assert r3.status_code == 200
    # header-token POST -> through (proved it holds the secret)
    r4 = asyncio.run(A._guard_sensitive_routes(Req("POST", {}, {"x-mongoose-token": "tok"}), nxt))
    assert r4.status_code == 200
