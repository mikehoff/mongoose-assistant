from __future__ import annotations

import json
from pathlib import Path


def test_claim_registry_seeded_and_observation_idempotent():
    from mongoose.db import init_db
    from mongoose.services import self_harden as H

    init_db()
    cls = H.claims()
    keys = {c["key"] for c in cls}
    assert "configured_truth" in keys
    assert "local_first_egress" in keys

    a = H.record_observation("test", "false_success", "setup", "configured but not usable", claim_key="configured_truth")
    b = H.record_observation("test", "false_success", "setup", "configured but not usable", claim_key="configured_truth")
    assert a["id"] == b["id"]
    st = H.status()
    assert st["open"] == 1
    assert st["top"][0]["claim_key"] == "configured_truth"


def test_scan_runtime_turns_audit_failure_into_observation():
    from mongoose.db import init_db, audit
    from mongoose.services import self_harden as H

    init_db()
    audit("rtm_management", "metro-import", False, reason="duplicate bank credit", payload={"row": 3})
    r = H.scan_runtime()
    assert r["observations"] >= 1
    rows = H.recent(10)
    assert any(o["claim_key"] == "rtm_ledger_integrity" and "metro-import" in o["scope"] for o in rows)


def test_module_index_rebuilds_code_map(tmp_path):
    from mongoose.db import init_db
    from mongoose.services import self_harden as H

    init_db()
    root = tmp_path / "repo"
    (root / "src" / "mongoose").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "mongoose" / "toy.py").write_text(
        '"""Toy module."""\nimport os\ndef f(sub):\n    sub.add_parser("toy")\n    os.getenv("TOY_URL")\n',
        encoding="utf-8",
    )
    (root / "tests" / "test_toy.py").write_text("def test_x(): pass\n", encoding="utf-8")
    r = H.rebuild_module_index(root)
    assert r["modules"] == 1
    rows = H.module_index(query="toy")
    assert rows and rows[0]["commands"] == ["toy"]
    assert "TOY_URL" in rows[0]["settings"]
    assert "test_toy.py" in rows[0]["tests"]


def test_export_packet_writes_markdown_and_json(tmp_path):
    from mongoose.db import init_db
    from mongoose.services import self_harden as H

    init_db()
    H.record_observation("user", "issue", "cockpit", "ready but not ready", severity="P0", claim_key="install_runtime_identity")
    out = H.export_packet(tmp_path / "packet", include_module_index=False)
    md = Path(out["md_path"])
    js = Path(out["json_path"])
    assert md.exists() and js.exists()
    assert "ready but not ready" in md.read_text(encoding="utf-8")
    payload = json.loads(js.read_text(encoding="utf-8"))
    assert payload["status"]["open_p0"] == 1


def test_hardening_plan_is_created_awaiting_approval():
    from mongoose.db import init_db
    from mongoose.services import self_harden as H

    init_db()
    obs = H.record_observation("test", "issue", "setup", "Setup claimed configured from placeholder", claim_key="configured_truth")
    plan = H.create_hardening_plan(obs["id"])
    assert plan["approved"] is False
    assert plan["name"].startswith("self-harden-")
    assert [s["id"] for s in plan["steps"]] == ["01-reproduce", "02-patch", "03-gate-and-report"]
    assert "placeholder" in plan["steps"][0]["task"]


def test_cockpit_harden_routes_are_read_and_scan_surfaces():
    from fastapi.testclient import TestClient
    from mongoose.app import app

    with TestClient(app) as client:
        r = client.get("/cockpit/harden")
        assert r.status_code == 200
        assert "open" in r.json()
        nonce = client.get("/cockpit/nonce").json()["cockpit_nonce"]
        s = client.post("/cockpit/harden/scan", headers={"X-Cockpit-Nonce": nonce})
        assert s.status_code == 200
        assert "observations" in s.json()
