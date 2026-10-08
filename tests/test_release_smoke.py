"""A fast release-smoke group: the highest-signal checks that must hold for any ship, chosen
to run in a few seconds so it is usable as a quick gate before the full ~2700-test suite
(external audit B5). Run with: pytest tests/test_release_smoke.py -q"""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def test_smoke_version_is_consistent():
    import mongoose
    import json
    import pathlib
    rel = json.loads(pathlib.Path("release.json").read_text())
    assert rel["version"] == mongoose.__version__


def test_smoke_persona_reports_true_version():
    import mongoose
    from mongoose.persona import persona_card
    assert mongoose.__version__ in persona_card() and "1.1 Beta" not in persona_card()


def test_smoke_all_public_sensitive_verbs_have_confirm_dispatch():
    """The recurring bug class: a sensitive tool wired for proposal but not for confirmed
    execution (self_patch shipped broken this way, so did wp_plugin author once)."""
    from mongoose.tools import SENSITIVE_TOOLS
    from mongoose.services import sensitive_tools as S
    import inspect
    src = inspect.getsource(S._execute) + inspect.getsource(S._execute_async)
    for tool in SENSITIVE_TOOLS:
        assert f'"{tool}"' in src or f"'{tool}'" in src, f"{tool} has no confirm dispatch branch"


def test_smoke_guest_gets_no_backend_detail_and_no_admin_text(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setenv("MONGOOSE_ALLOW_REMOTE", "true")
    monkeypatch.setenv("MONGOOSE_HOST", "0.0.0.0")
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "t")
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://example.duckdns.org")
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app, client=("203.0.113.9", 5000))
    from mongoose.guest_invites import create_guest_invite, claim_guest_invite
    inv = create_guest_invite(owner_label="g")
    tok = inv["invite_url"].rstrip("/").split("/")[-1]
    cl = claim_guest_invite(tok, {"mongoose_name": "Lee", "public_name": "R", "fun_fact": "x"})
    H = {"X-Mongoose-Device-Token": cl["device_token"]}
    j = c.post("/device/chat", headers=H, json={"message": "hi"}).json()
    assert "error_detail" not in j
    r = c.get("/cockpit/devices", headers=H)
    assert "MONGOOSE_ALLOW_REMOTE" not in r.text


def test_smoke_rtm_placeholder_demand_excluded_from_portal(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "r.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    from mongoose.db import init_db
    init_db()
    from mongoose.services import rtm_management as RM
    from mongoose.services import rtm_portal as RP
    RM.seed_flats()
    RM.create_demand(document_number="D1", flat="1ECF1", period="p",
                     amount="1.00", issue_date="2026-07-04", due_date="2026-08-01")
    rows = RP._demand_rows_for_snapshot()
    assert not any(d["document_number"] == "D1" for d in rows)


def test_smoke_no_space_comma_artefacts():
    import ast
    import pathlib
    for p in pathlib.Path("src/mongoose").rglob("*.py"):
        if "pycache" in str(p):
            continue
        for node in ast.walk(ast.parse(p.read_text())):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and " , " in node.value):
                raise AssertionError(f"space-comma artefact in {p}:{node.lineno}")
