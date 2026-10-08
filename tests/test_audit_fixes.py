"""Fixes from the 1.28.2 logic+UX audit (a second model's full test pass, not just compile):

1. Approvals contract drift: phone cockpit rendered buttons that silently did nothing
   because they read legacy it.approve/it.cancel fields that the backend stopped emitting.
   Phone renderer now uses actions[] like the desktop, with both the ok:false check and
   the ok HTTP-status check, and the legacy silent-bail is gone.

2. False-green action helpers: UI callers removed approval cards on HTTP 200 even when
   the JSON body said {ok:false}. Desktop runApproval now checks both; phone api() helper
   already returned parsed JSON so !r.ok already covered ok:false -- confirmed.

3. JSON body crash: five mutating cockpit endpoints crashed with a JSONDecodeError on a
   no-body POST instead of returning a clean validation message. All now wrapped.

4. CLI pathlib local import: function-local import inside a large if/elif chain can
   poison sibling branches; moved to module scope."""
from __future__ import annotations
import warnings
warnings.filterwarnings("ignore")


def _c(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "a.sqlite3"))
    from mongoose.db import init_db; init_db()
    from fastapi.testclient import TestClient
    import mongoose.app as A
    return TestClient(A.app), A._COCKPIT_NONCE


def test_no_body_post_returns_validation_not_crash(monkeypatch, tmp_path):
    c, NONCE = _c(monkeypatch, tmp_path)
    h = {"x-cockpit-nonce": NONCE}
    for route in ["/cockpit/knowledge/care/activate", "/cockpit/knowledge/care/pause",
                  "/cockpit/knowledge/review/resolve", "/cockpit/knowledge/insight/feedback"]:
        r = c.post(route, headers=h)
        assert r.status_code == 200 and r.json()["ok"] is False, \
            f"{route} should return ok:false on no body, got {r.status_code} {r.text}"


def test_approvals_always_emits_actions_list(monkeypatch, tmp_path):
    c, _ = _c(monkeypatch, tmp_path)
    r = c.get("/cockpit/approvals")
    assert r.status_code == 200
    d = r.json()
    assert "approvals" in d and "counts" in d
    for item in d["approvals"]:
        assert "actions" in item and isinstance(item["actions"], list), \
            f"approval item missing actions[]: {item}"
        assert "approve" not in item, f"legacy 'approve' field still present: {item}"


def test_phone_approvals_uses_actions_and_checks_ok_false():
    from mongoose.cockpit import PHONE_COCKPIT_HTML as H
    assert "__phoneApActions" in H, "phone must use the actions[] array"
    assert "runPhoneApproval" in H
    assert "if(!r||!r.ok)" in H, \
        "phone runner must check ok:false (api() returns parsed JSON, so r.ok is the JSON field)"
    assert "!it.approve" not in H, "legacy silent bail on missing approve field must be gone"
    assert "Nothing was changed" in H


def test_desktop_approval_runner_checks_ok_false():
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert "async function runApproval(idx)" in h
    assert "postActionOk(ac.url" in h, \
        "desktop runApproval must go through the honest postActionOk helper, which throws on ok:false"
    assert "Action not completed" in h


def test_cli_pathlib_at_module_scope():
    import inspect
    import mongoose.cli as CLI
    src = inspect.getsource(CLI)
    lines = src.split('\n')
    # Must appear near the top (before line 20)
    first = next(i for i, l in enumerate(lines) if 'import pathlib' in l)
    assert first < 20, f"pathlib import is on line {first}, not at module top"
    # Must appear exactly once -- no function-local duplicate
    assert src.count('import pathlib') == 1, "function-local 'import pathlib' still present"
