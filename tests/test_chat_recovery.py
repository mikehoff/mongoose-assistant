"""Chat history recovers the last exchanges for EVERY participant — the owner on the main thread,
each guest on their own isolated thread — and survives a restart, with no cross-leak."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def _pair_guest(name="Jo"):
    import mongoose.devices as DEV
    pc = DEV.create_pairing_code("trusted_guest")
    return DEV.claim_pairing_code(pc["code"], device_name=name, device_type="phone")


def test_guest_history_endpoint_serves_own_thread_not_owners():
    """The bug this guards: /device/chat/history hardcoded the owner conversation, leaking the owner's
    history to guests and hiding the guest's own."""
    _fresh()
    from mongoose.services import memory as M
    M.ensure_conversation("mongoose-main")
    M.add_message("mongoose-main", "assistant", "OWNER_PRIVATE_CONTENT_XYZ")
    res = _pair_guest()
    gid, tok = res["device_id"], res["device_token"]
    M.ensure_conversation(f"guest:{gid}")
    M.add_message(f"guest:{gid}", "assistant", "GUEST_OWN_CONTENT_ABC")

    from fastapi.testclient import TestClient
    from mongoose.app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/device/chat/history", headers={"X-Mongoose-Device-Token": tok})
    body = r.json()
    texts = " ".join(m.get("content", "") for m in body.get("messages", []))
    assert "OWNER_PRIVATE_CONTENT_XYZ" not in texts, "guest history leaked owner's conversation"
    assert "GUEST_OWN_CONTENT_ABC" in texts, "guest did not recover its own history"
    assert body.get("conversation") == f"guest:{gid}"


def test_owner_history_unaffected():
    _fresh()
    from mongoose.services import memory as M
    M.ensure_conversation("mongoose-main")
    M.add_message("mongoose-main", "assistant", "owner content here")
    # An owner-profile device gets the main conversation
    import mongoose.devices as DEV
    pc = DEV.create_pairing_code("primary_phone")
    res = DEV.claim_pairing_code(pc["code"], device_name="AlexPhone", device_type="phone")
    from fastapi.testclient import TestClient
    from mongoose.app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/device/chat/history", headers={"X-Mongoose-Device-Token": res["device_token"]})
    body = r.json()
    texts = " ".join(m.get("content", "") for m in body.get("messages", []))
    assert "owner content here" in texts
    assert body.get("conversation") == "mongoose-main"


def test_both_participants_recover_last_exchanges_after_restart():
    _fresh()
    from mongoose.services import memory as M
    M.ensure_conversation("mongoose-main")
    for i in range(4):
        M.add_message("mongoose-main", "user", f"alex {i}")
        M.add_message("mongoose-main", "assistant", f"to alex {i}")
    res = _pair_guest()
    gid = res["device_id"]
    M.ensure_conversation(f"guest:{gid}")
    for i in range(3):
        M.add_message(f"guest:{gid}", "user", f"jo {i}")
        M.add_message(f"guest:{gid}", "assistant", f"to jo {i}")

    # Simulate restart: clear caches, run wake-up
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    import mongoose.db as DB
    DB._SCHEMA_READY.clear()
    from mongoose.brain_wakeup import wake_up
    wake_up()

    alex = M.recent_messages("mongoose-main", limit=6)
    jo = M.recent_messages(f"guest:{gid}", limit=6)
    assert len(alex) == 6 and len(jo) == 6
    # No cross-contamination
    assert not any("jo" in m["content"] for m in alex)
    assert not any("alex" in m["content"] for m in jo)


def test_two_guests_recover_separate_threads():
    _fresh()
    from mongoose.services import memory as M
    a = _pair_guest("Alice")
    b = _pair_guest("Bob")
    M.ensure_conversation(f"guest:{a['device_id']}")
    M.ensure_conversation(f"guest:{b['device_id']}")
    M.add_message(f"guest:{a['device_id']}", "assistant", "ALICE_THREAD")
    M.add_message(f"guest:{b['device_id']}", "assistant", "BOB_THREAD")

    from fastapi.testclient import TestClient
    from mongoose.app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        ra = c.get("/device/chat/history", headers={"X-Mongoose-Device-Token": a["device_token"]})
        rb = c.get("/device/chat/history", headers={"X-Mongoose-Device-Token": b["device_token"]})
    ta = " ".join(m["content"] for m in ra.json()["messages"])
    tb = " ".join(m["content"] for m in rb.json()["messages"])
    assert "ALICE_THREAD" in ta and "BOB_THREAD" not in ta
    assert "BOB_THREAD" in tb and "ALICE_THREAD" not in tb
