"""Owner-controlled device merge: fold a duplicate 'same person' device into the
surviving one. The point of these tests is the accident-hardening: a merge must never
cross profiles, never target a dead device, and never fire without the owner echoing
both device names exactly. The happy path must consolidate that person's messages,
receipts, push subscriptions, and capabilities onto the survivor and revoke the source."""
import mongoose.devices as D
from mongoose.db import connect


def _pair(name: str, profile: str = "primary_phone") -> str:
    code = D.create_pairing_code(profile)
    claim = D.claim_pairing_code(code["code"], name, "android")
    assert claim["ok"] is True
    return claim["device_id"]


def _seed_state(device_id: str, label: str) -> None:
    """Give a device some attributed state so we can prove the merge moves it."""
    with connect() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS push_subscriptions (sub_id TEXT PRIMARY KEY, endpoint TEXT, "
            "p256dh TEXT, auth TEXT, user_id TEXT, device_id TEXT, device_profile TEXT, "
            "device_label TEXT, created_at TEXT, last_ok_at TEXT, fail_count INTEGER DEFAULT 0)")
        con.execute(
            "INSERT OR IGNORE INTO conversations(id, title, created_at, updated_at) "
            "VALUES ('main', 'main', '2026-07-06T00:00:00+00:00', '2026-07-06T00:00:00+00:00')")
        con.execute(
            "INSERT INTO messages(conversation_id, role, content, created_at, sender_device_id, sender_label) "
            "VALUES ('main', 'user', ?, '2026-07-06T00:00:00+00:00', ?, ?)",
            (f"hello from {label}", device_id, label))
        con.execute(
            "INSERT INTO push_subscriptions(sub_id, endpoint, p256dh, auth, user_id, device_id, "
            "device_profile, device_label, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"sub-{device_id}", f"https://push/{device_id}", "k", "a", "", device_id,
             "primary_phone", label, "2026-07-06T00:00:00+00:00"))
        con.commit()


def test_merge_happy_path_consolidates_and_revokes_source():
    old = _pair("Alexs old phone")
    new = _pair("Alexs new phone")
    _seed_state(old, "Alexs old phone")
    D.set_device_capabilities(old, ["approvals"])

    res = D.merge_devices(old, new,
                          confirm_source_name="Alexs old phone",
                          confirm_target_name="Alexs new phone")
    assert res["ok"] is True, res
    assert res["source_revoked"] is True
    assert res["moved"]["messages"] == 1
    assert res["moved"]["push_subscriptions"] == 1

    with connect() as con:
        # source is revoked, target still live
        src = con.execute("SELECT revoked FROM paired_devices WHERE device_id=?", (old,)).fetchone()
        tgt = con.execute("SELECT revoked FROM paired_devices WHERE device_id=?", (new,)).fetchone()
        assert src["revoked"] == 1
        assert tgt["revoked"] == 0
        # the person's message now belongs to the survivor, NOT quarantined
        m = con.execute("SELECT conversation_id, sender_device_id FROM messages "
                        "WHERE content='hello from Alexs old phone'").fetchone()
        assert m["sender_device_id"] == new
        assert m["conversation_id"] == "main"
        # push subscription re-pointed
        p = con.execute("SELECT device_id FROM push_subscriptions WHERE sub_id=?",
                        (f"sub-{old}",)).fetchone()
        assert p["device_id"] == new


def test_merge_refuses_across_profiles():
    owner = _pair("Owner phone", "primary_phone")
    guest = _pair("Guest phone", "trusted_guest")
    res = D.merge_devices(guest, owner,
                          confirm_source_name="Guest phone",
                          confirm_target_name="Owner phone")
    assert res["ok"] is False
    assert "profile" in res["error"]
    # neither device was touched
    with connect() as con:
        for did in (owner, guest):
            r = con.execute("SELECT revoked FROM paired_devices WHERE device_id=?", (did,)).fetchone()
            assert r["revoked"] == 0


def test_merge_requires_exact_name_confirmation():
    a = _pair("Device A")
    b = _pair("Device B")
    # wrong name echoed: refuse
    res = D.merge_devices(a, b, confirm_source_name="Device A", confirm_target_name="WRONG")
    assert res["ok"] is False
    assert "confirmation" in res["error"]
    with connect() as con:
        r = con.execute("SELECT revoked FROM paired_devices WHERE device_id=?", (a,)).fetchone()
        assert r["revoked"] == 0  # nothing happened


def test_merge_rejects_self_and_unknown_and_dead_target():
    a = _pair("Solo device")
    assert D.merge_devices(a, a, confirm_source_name="Solo device",
                           confirm_target_name="Solo device")["ok"] is False
    assert D.merge_devices(a, "nope", confirm_source_name="Solo device",
                           confirm_target_name="x")["ok"] is False
    # revoked target is refused
    b = _pair("Dead target")
    D.revoke_device(b)
    res = D.merge_devices(a, b, confirm_source_name="Solo device", confirm_target_name="Dead target")
    assert res["ok"] is False
    assert "revoked" in res["error"]


def test_merge_name_confirmation_is_space_and_case_insensitive():
    a = _pair("Case Phone")
    b = _pair("Target Phone")
    res = D.merge_devices(a, b, confirm_source_name="  case   phone ",
                          confirm_target_name="TARGET PHONE")
    assert res["ok"] is True, res
