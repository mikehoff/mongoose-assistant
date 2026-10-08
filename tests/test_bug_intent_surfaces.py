"""'raise bug' / 'report a bug: X' is the designed bug-intake feature and must fire wherever it is
typed , the OWNER in the lounge, and a guest , landing a real bug in the owner's queue. Previously
it was guest-only and wired into only one route, so the owner's 'raise bug' did nothing special."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def _setup(monkeypatch, tmp_path):
    for k, v in {"MONGOOSE_DB_PATH": str(tmp_path / "b.sqlite3"),
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


def _owner(c):
    from mongoose import devices
    code = devices.create_pairing_code(profile="primary_phone")["code"]
    return {"X-Mongoose-Device-Token": devices.claim_pairing_code(code, "Alex", "phone", display_name="Alex")["device_token"]}


def test_owner_raise_bug_in_lounge_logs_a_bug(monkeypatch, tmp_path):
    c = _setup(monkeypatch, tmp_path)
    H = _owner(c)
    from mongoose.services.bug_workflow import list_open_bugs
    before = len(list_open_bugs(limit=50))
    r = c.post("/device/irc/send", headers=H,
               json={"text": "report a bug: the poke button does nothing"}).json()
    assert r["ok"] and "logged that" in r["reply"].lower()
    after = list_open_bugs(limit=50)
    assert len(after) == before + 1
    assert any("poke button" in (b.get("summary") or b.get("text") or "") for b in after)
