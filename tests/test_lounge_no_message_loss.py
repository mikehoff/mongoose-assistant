"""Regression: participants must never lose each other's lounge messages. The original bug was a
client that, after sending, jumped its poll cursor to the room's newest id, skipping any lines
others posted while it was typing. This locks down the SERVER contract the fixed client relies on:
history?since_id=<my old cursor> returns EVERY later message from EVERY participant, in id order."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def _setup(monkeypatch, tmp_path):
    for k, v in {"MONGOOSE_DB_PATH": str(tmp_path / "n.sqlite3"),
                 "MONGOOSE_DATA_DIR": str(tmp_path / "d"),
                 "MONGOOSE_ALLOW_REMOTE": "true", "MONGOOSE_HOST": "0.0.0.0",
                 "MONGOOSE_API_TOKEN": "t", "MONGOOSE_LOUNGE_STARTERS": "off",
                 "MONGOOSE_PUBLIC_URL": "https://example.duckdns.org"}.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()
    import mongoose.services.lounge_surpriser as _LS
    monkeypatch.setattr(_LS, "is_messaging_alone", lambda device_id: False)
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app)


def _guest(c, name):
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    inv = create_guest_invite(owner_label=name)
    tok = inv["invite_url"].rstrip("/").split("/")[-1]
    cl = claim_guest_invite(tok, {"mongoose_name": name, "public_name": name, "fun_fact": "x"})
    return {"x-mongoose-device-token": cl["device_token"]}


def _texts(c, headers, since_id):
    r = c.get(f"/device/irc/history?since_id={since_id}", headers=headers).json()
    return [m["text"] for m in r.get("messages", []) if m.get("text")]


def test_interleaved_sends_lose_no_participant_messages(monkeypatch, tmp_path):
    c = _setup(monkeypatch, tmp_path)
    A = _guest(c, "Jo")
    B = _guest(c, "Sam")

    # A opens the room and catches up: this is A's poll cursor.
    hist = c.get("/device/irc/history?since_id=0", headers=A).json()["messages"]
    a_cursor = hist[-1]["id"] if hist else 0

    # B posts while A is "typing" (A has NOT polled since a_cursor).
    c.post("/device/irc/send", headers=B, json={"text": "B-one"})
    c.post("/device/irc/send", headers=B, json={"text": "B-two"})
    # A now sends its own line.
    c.post("/device/irc/send", headers=A, json={"text": "A-mine"})

    # The fixed client re-polls from a_cursor (NOT from the room max). It must see BOTH of B's
    # lines and its own, in order , nothing skipped.
    seen = _texts(c, A, a_cursor)
    for expected in ("B-one", "B-two", "A-mine"):
        assert expected in seen, f"{expected} missing from {seen}"
    assert seen.index("B-one") < seen.index("B-two") < seen.index("A-mine")

    # And B, polling incrementally from its own last-seen, still receives A's line.
    assert "A-mine" in _texts(c, B, 0)
