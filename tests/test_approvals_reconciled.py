"""Reconciles the external audit's approvals findings against what is ACTUALLY in this tree
(a prior turn's edits partially did not survive into the shipped zip; this re-verifies from
scratch rather than assuming continuity):

1. Backend emits BOTH the generic actions[] contract and legacy flat approve/cancel keys for
   URL-only pairs (whatsapp draft, bug), so old and current clients both work. Proven live: a
   real draft_message() creates a draft, /cockpit/approvals returns both shapes with matching
   URLs.
2. Desktop: postActionOk is the honest chokepoint (throws on {ok:false} or non-2xx with the
   server's reason); runApproval is consolidated onto it. Proven in a real DOM: ok:false keeps
   the card and surfaces the reason, ok:true removes it and reloads.
3. Phone: loadApprovals reads the real actions[] contract (with legacy fallback);
   runPhoneApproval independently reached honest ok:false handling. Proven in a real DOM with a
   stateful server mock: ok:false keeps the card and surfaces the reason, ok:true removes it.
4. care/activate and review/resolve do not crash on an absent/malformed JSON body (verified via
   a different, equivalent mechanism -- inline try/except -- to the one originally proposed;
   what matters is the endpoint never 500s, which is what's tested here).
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import warnings

import pytest

warnings.filterwarnings("ignore")


def test_backend_emits_both_action_contracts(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "a.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from mongoose.services.whatsapp import draft_message
    draft_message("Jo", "hello")
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    r = c.get("/cockpit/approvals", headers={"x-cockpit-nonce": A._COCKPIT_NONCE})
    wa = next(a for a in r.json()["approvals"] if a["kind"] == "whatsapp_draft")
    assert "actions" in wa and "approve" in wa and "cancel" in wa
    assert wa["approve"] == wa["actions"][0]["url"]


def test_care_activate_and_review_resolve_never_500_on_bad_body(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "b.sqlite3"))
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    N = {"x-cockpit-nonce": A._COCKPIT_NONCE}
    for path in ("/cockpit/knowledge/care/activate", "/cockpit/knowledge/review/resolve"):
        assert c.post(path, headers=N).status_code == 200
        assert c.post(path, headers=N, content="not json {{").status_code == 200


def _node_env():
    jsdom = pathlib.Path("/home/claude/node_modules/jsdom")
    return {"JSDOM_PATH": str(jsdom) if jsdom.exists() else "jsdom", "PATH": "/usr/bin:/bin"}


def _skip_if_no_node_jsdom():
    node = shutil.which("node")
    if not node:
        return None, None
    env = _node_env()
    if subprocess.run([node, "-e", "require(process.env.JSDOM_PATH || 'jsdom')"],
                      env=env, capture_output=True).returncode != 0:
        return None, None
    return node, env


def test_desktop_approval_honesty_dom(tmp_path):
    node, env = _skip_if_no_node_jsdom()
    if node is None:
        pytest.skip("node/jsdom not available")
    src = pathlib.Path("src/mongoose/cockpit.py").read_text()
    p_start = src.index("async function postAction(url")
    p_end = src.index("\nconst TABS", p_start)
    r_start = src.index("async function runApproval(idx)")
    r_end = src.index("\nasync function tuneAct", r_start)
    block = tmp_path / "desktop_approvals.js"
    block.write_text(src[p_start:p_end] + "\n" + src[r_start:r_end])
    r = subprocess.run([node, "tests/assets/desktop_approvals_journeys.js", str(block)],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL 2 DESKTOP APPROVAL HONESTY JOURNEYS PASS" in r.stdout


def test_phone_approval_honesty_dom(tmp_path):
    node, env = _skip_if_no_node_jsdom()
    if node is None:
        pytest.skip("node/jsdom not available")
    src = pathlib.Path("src/mongoose/cockpit.py").read_text()
    idx = src.rindex("async function loadApprovals(){\n  var root")
    end = src.index("\n// ---- Scheduler ----", idx)
    block = tmp_path / "phone_approvals.js"
    block.write_text(src[idx:end])
    r = subprocess.run([node, "tests/assets/phone_approvals_journeys.js", str(block)],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL 3 PHONE APPROVAL HONESTY JOURNEYS PASS" in r.stdout


def test_approve_and_cancelAct_dead_code_removed():
    """These had zero callers anywhere and a less-safe implementation than the live path
    (runApproval/postActionOk); dead code with a stale contract is worse than no code."""
    src = pathlib.Path("src/mongoose/cockpit.py").read_text()
    assert "async function approve(url)" not in src
    assert "async function cancelAct(url)" not in src


def test_revoke_device_honesty_dom(tmp_path):
    """Device revocation is security-relevant: a silent ok:false would leave someone believing
    a lost/stolen phone's access was cut off when the server refused the request."""
    node, env = _skip_if_no_node_jsdom()
    if node is None:
        pytest.skip("node/jsdom not available")
    src = pathlib.Path("src/mongoose/cockpit.py").read_text()
    p_start = src.index("async function postAction(url")
    p_end = src.index("\nconst TABS", p_start)
    r_start = src.index("async function revokeDevice")
    r_end = src.index("\n}\n", r_start) + 2
    block = tmp_path / "revoke.js"
    block.write_text(src[p_start:p_end] + "\n" + src[r_start:r_end])
    r = subprocess.run([node, "tests/assets/revoke_journeys.js", str(block)],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL 2 REVOKE HONESTY JOURNEYS PASS" in r.stdout
