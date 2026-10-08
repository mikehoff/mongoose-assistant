from __future__ import annotations

import os

import tempfile

def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    for k in [k for k in os.environ if k.startswith("MONGOOSE_METRO")]:
        os.environ.pop(k)
    from mongoose.db import init_db
    init_db()
    return d

def test_rtm_bank_feed_tool_and_status_area_are_readable():
    _fresh()
    import asyncio
    from mongoose.tools import TOOLS
    from mongoose.services.status_tools import tool_system_status
    assert "rtm_bank_feed_playbook" in TOOLS
    out = asyncio.run(TOOLS["rtm_bank_feed_playbook"]["executor"]({"provider":"crunch"}))
    assert out["ok"] is True
    assert out["playbook"]["name"] == "Crunch Plus"
    status = asyncio.run(tool_system_status({"area":"rtm_bank_feeds"}))
    assert status["area"] == "rtm_bank_feeds"
    assert status["ok"] is True

def test_bank_feed_read_routes_expose_playbook():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app)
    s = c.get("/rtm/bank-feed/status")
    assert s.status_code == 200
    assert s.json()["title"] == "RTM Metro bank-feed verification playbook"
    p = c.get("/rtm/bank-feed/playbook?provider=xero")
    assert p.status_code == 200
    assert p.json()["provider"] == "xero"
    m = c.get("/rtm/bank-feed/messages/metro_business")
    assert m.status_code == 200
    assert "exact Metro product name" in m.json()["message"]
