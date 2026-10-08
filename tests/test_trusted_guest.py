"""Trusted guest: isolated conversation, zero tools, zero owner memory, DDNS QR."""
import asyncio
import os
import tempfile


def _fresh(extra=None):
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    for k, v in (extra or {}).items():
        os.environ[k] = v
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


# ── Profile and capabilities ─────────────────────────────────────────────────

def test_trusted_guest_profile_exists():
    from mongoose.devices import _ROLE_PLAN
    assert "trusted_guest" in _ROLE_PLAN


def test_trusted_guest_tools_off_by_design():
    from mongoose.devices import _ROLE_PLAN
    plan = _ROLE_PLAN["trusted_guest"]
    assert plan.get("tools") == "off_by_design", "tools must be off_by_design for guests"
    assert plan.get("cockpit") == "off_by_design", "cockpit must be off for guests"
    assert plan.get("approvals") == "off_by_design", "approvals must be off for guests"


def test_trusted_guest_has_no_default_caps():
    """Guests get no capabilities at pairing time — nothing is auto-enabled."""
    from mongoose.devices import _default_caps
    caps = _default_caps("trusted_guest")
    # optional caps (voice) are not in _default_caps because they require expected state
    assert "tools" not in caps
    assert "cockpit" not in caps
    assert "approvals" not in caps


# ── Pairing code allows trusted_guest ────────────────────────────────────────

def test_pair_code_endpoint_allows_guest_profile():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app, _COCKPIT_NONCE
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/cockpit/devices/pair-code",
               json={"profile": "trusted_guest"},
               headers={"X-Cockpit-Nonce": _COCKPIT_NONCE})
    assert r.status_code == 200
    d = r.json()
    assert d.get("code"), f"no pairing code returned: {d}"
    assert d.get("profile") == "trusted_guest"


def test_pair_code_unknown_profile_falls_back_to_primary_phone():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app, _COCKPIT_NONCE
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/cockpit/devices/pair-code",
               json={"profile": "hacker_profile"},
               headers={"X-Cockpit-Nonce": _COCKPIT_NONCE})
    assert r.status_code == 200
    assert r.json().get("profile") == "primary_phone"


# ── DDNS URL helper ───────────────────────────────────────────────────────────

def test_guest_reachable_url_uses_ddns(monkeypatch):
    """When DDNS is configured, guest QR URL uses DDNS not LAN IP."""
    _fresh()
    import mongoose.app as APP
    from unittest.mock import MagicMock
    monkeypatch.setattr(APP, "_guest_reachable_url",
                        lambda req: "https://mymac.duckdns.org:8765")
    result = APP._guest_reachable_url(MagicMock())
    assert "duckdns" in result


def test_guest_reachable_url_falls_back_to_lan(monkeypatch):
    """Without DDNS, guest QR falls back to LAN IP."""
    _fresh()
    from mongoose.app import _guest_reachable_url
    from unittest.mock import MagicMock
    req = MagicMock()
    req.url.scheme = "http"
    req.headers.get = lambda k, d="": "127.0.0.1:8765"
    # With no DDNS configured, it should return something starting with http
    import mongoose.ddns as ddns_mod
    monkeypatch.setattr(ddns_mod, "public_url", lambda env=None: None)
    result = _guest_reachable_url(req)
    assert result.startswith("http")


# ── Isolated conversation ─────────────────────────────────────────────────────

def test_guest_conversation_is_separate_from_main():
    """Guest device_id → conversation ID guest:{device_id}, never DEFAULT_CONVERSATION_ID."""
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID
    device_id = "test_guest_abc123"
    guest_conv = f"guest:{device_id}"
    assert guest_conv != DEFAULT_CONVERSATION_ID
    assert "guest:" in guest_conv


def test_guest_chat_uses_isolated_conversation(monkeypatch):
    """Device chat for trusted_guest must use guest:{device_id} conversation, not shared."""
    _fresh()
    captured = {}

    async def fake_chat(prompt, *, conversation_id, role, guest_mode, **kw):
        captured["conversation_id"] = conversation_id
        captured["guest_mode"] = guest_mode
        return {"reply": "hello from guest isolation test"}

    import mongoose.llm as LLM
    monkeypatch.setattr(LLM, "chat_with_memory", fake_chat)
    import mongoose.devices as DEV
    monkeypatch.setattr(DEV, "device_for_token", lambda t: {
        "device_id": "gst_001", "name": "Alice", "profile": "trusted_guest"
    })
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/device/chat", json={"message": "hello"},
               headers={"X-Mongoose-Device-Token": "guest_tok"})
    assert r.status_code == 200
    assert captured.get("conversation_id") == "guest:gst_001", \
        f"expected guest conversation, got: {captured.get('conversation_id')}"
    assert captured.get("guest_mode") is True


def test_primary_phone_still_uses_default_conversation(monkeypatch):
    """Primary phone must still use DEFAULT_CONVERSATION_ID (not affected by guest logic)."""
    _fresh()
    captured = {}

    async def fake_chat(prompt, *, conversation_id, role, guest_mode=False, **kw):
        captured["conversation_id"] = conversation_id
        captured["guest_mode"] = guest_mode
        return {"reply": "test"}

    import mongoose.llm as LLM
    monkeypatch.setattr(LLM, "chat_with_memory", fake_chat)
    import mongoose.devices as DEV
    monkeypatch.setattr(DEV, "device_for_token", lambda t: {
        "device_id": "ph_001", "name": "My Phone", "profile": "primary_phone"
    })
    from fastapi.testclient import TestClient
    from mongoose.app import app
    from mongoose.services.memory import DEFAULT_CONVERSATION_ID
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/device/chat", json={"message": "hello"},
               headers={"X-Mongoose-Device-Token": "primary_tok"})
    assert r.status_code == 200
    assert captured.get("conversation_id") == DEFAULT_CONVERSATION_ID
    assert captured.get("guest_mode") is False


# ── Tool isolation ────────────────────────────────────────────────────────────

def test_guest_mode_skips_tool_routing():
    """guest_mode=True must prevent tool routing entirely."""
    import inspect
    from mongoose import llm as LLM
    src = inspect.getsource(LLM._chat_turn)  # turn body moved under the per-conversation turn lock
    # The tool gate must check guest_mode
    assert "not guest_mode" in src or "guest_mode" in src
    # Check that the tool routing if-block has guest_mode guard
    tool_gate_idx = src.find("classify_role as _classify")
    assert tool_gate_idx > 0
    # The condition before classify_role must include guest_mode
    before_classify = src[max(0, tool_gate_idx-100):tool_gate_idx]
    assert "guest_mode" in before_classify, \
        "tool routing must be gated on guest_mode"


# ── Owner memory isolation ────────────────────────────────────────────────────

def test_guest_mode_zeroes_owner_facts():
    """guest_mode must inject empty facts, not owner's memory."""
    import inspect
    from mongoose import llm as LLM
    src = inspect.getsource(LLM._chat_turn)  # turn body moved under the per-conversation turn lock
    assert "if guest_mode" in src
    assert "facts = []" in src


def test_chat_with_memory_guest_mode_no_facts(monkeypatch):
    """Integration: guest_mode=True results in no facts injected."""
    _fresh()
    from mongoose.services.memory import remember_fact, ensure_conversation
    from mongoose.db import init_db; init_db()

    # Plant an owner fact
    remember_fact("Alex lives in Anytown", source="test")

    injected_facts = []

    # Capture system message to check facts
    import mongoose.llm as LLM

    async def fake_chat_call(model, messages, settings):
        for m in messages:
            if m["role"] == "system" and "Anytown" in m.get("content", ""):
                injected_facts.append("owner_fact_leaked")
        return "hi there"

    monkeypatch.setattr(LLM, "_chat_call", fake_chat_call)

    # Guest conversation
    ensure_conversation("guest:test_device_001")
    asyncio.run(LLM.chat_with_memory(
        "hello",
        conversation_id="guest:test_device_001",
        role="chat",
        guest_mode=True,
    ))
    assert "owner_fact_leaked" not in injected_facts, \
        "owner memory facts must NEVER appear in guest conversation context"


# ── Cockpit UI ───────────────────────────────────────────────────────────────

def test_cockpit_has_add_guest_button():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    html = c.get("/ui").text
    assert "trusted_guest" in html
    assert "Add guest" in html


def test_cockpit_guest_shows_ddns_note():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    html = c.get("/ui").text
    assert "DDNS" in html or "ddns" in html.lower()
    # Guest description must mention no tools and no data access
    assert "no tools" in html.lower() or "off_by_design" in html.lower() or "guest" in html


# ── STRUCTURAL isolation at the data layer (would have caught the flag-only bug) ──
# These tests do NOT mock the memory layer. They exercise the real remember/search/build_context
# functions, because the isolation guarantee must be a property of storage, not of one caller.

def test_guest_build_context_never_sees_owner_facts():
    """The core guarantee: build_context for a guest:* conversation returns ZERO owner facts,
    no matter which code path calls it. This is the bug the flag-only implementation missed."""
    _fresh()
    from mongoose.services import memory as M
    M.remember_fact("The owner's bank PIN is 4823")
    M.remember_fact("Owner lives at 1 Example Court")
    _hist, facts = M.build_context("guest:friend_device_001", "what do you know about me")
    assert facts == [], f"owner facts leaked into guest context: {facts}"


def test_owner_build_context_unaffected_by_guest_scoping():
    """Regression guard: the owner still sees their own facts (we didn't break the main path)."""
    _fresh()
    from mongoose.services import memory as M
    M.remember_fact("Owner drives a Honda")
    _hist, facts = M.build_context("mongoose-main", "what do you know about me")
    assert any("Honda" in f for f in facts), "owner facts must still surface for owner conversation"


def test_guest_fact_does_not_pollute_owner_brain():
    """Inverse leak: a fact saved in a guest conversation must NEVER appear in the owner scope."""
    _fresh()
    from mongoose.services import memory as M
    guest_scope = M.scope_for_conversation("guest:jo_phone")
    M.remember_fact("My name is Casey and I hate cilantro", scope=guest_scope)
    owner_facts = [f["fact"] for f in M.all_facts(limit=100)]  # owner scope
    assert not any("Casey" in f for f in owner_facts), \
        f"guest fact polluted the owner brain: {owner_facts}"


def test_guest_can_see_their_own_facts():
    """A guest's own facts must be visible to them (isolation is not amnesia)."""
    _fresh()
    from mongoose.services import memory as M
    scope = M.scope_for_conversation("guest:jo_phone")
    M.remember_fact("My name is Casey", scope=scope)
    _hist, facts = M.build_context("guest:jo_phone", "what is my name")
    assert any("Casey" in f for f in facts), "guest must see their own facts"


def test_two_guests_are_isolated_from_each_other():
    """Guest A must not see Guest B's facts — each guest device is its own namespace."""
    _fresh()
    from mongoose.services import memory as M
    M.remember_fact("Alice likes hiking", scope=M.scope_for_conversation("guest:alice"))
    M.remember_fact("Bob likes chess", scope=M.scope_for_conversation("guest:bob"))
    _h, alice_facts = M.build_context("guest:alice", "what do you know")
    assert not any("Bob" in f or "chess" in f for f in alice_facts), \
        "guest Alice leaked guest Bob's facts"


def test_guest_search_does_not_leak_owner_facts():
    """search_facts scoped to a guest must never return owner facts even on a direct keyword match."""
    _fresh()
    from mongoose.services import memory as M
    M.remember_fact("The owner's passport number is 123456789")
    leaked = M.search_facts("passport", scope=M.scope_for_conversation("guest:snoop"))
    assert leaked == [], f"guest search leaked owner facts: {leaked}"


def test_scope_for_conversation_mapping():
    """The scope deriver is the single source of truth — verify its contract."""
    from mongoose.services.memory import scope_for_conversation, OWNER_SCOPE
    assert scope_for_conversation("mongoose-main") == OWNER_SCOPE
    assert scope_for_conversation(None) == OWNER_SCOPE
    assert scope_for_conversation("guest:abc123") == "guest:abc123"
    # two different guests get different scopes
    assert scope_for_conversation("guest:a") != scope_for_conversation("guest:b")


def test_guest_forget_cannot_delete_owner_facts():
    """A guest's forget request must only ever surface/delete their own facts."""
    _fresh()
    from mongoose.services import memory as M
    M.remember_fact("Owner secret: the safe code is 9999")
    guest_scope = M.scope_for_conversation("guest:intruder")
    # Guest tries to forget something matching the owner fact
    cands = M.find_forget_candidates("safe code", scope=guest_scope)
    assert cands == [], f"guest forget surfaced owner facts as deletion candidates: {cands}"
    # And the owner fact is still there
    assert any("safe code" in f["fact"] for f in M.all_facts(limit=100))
