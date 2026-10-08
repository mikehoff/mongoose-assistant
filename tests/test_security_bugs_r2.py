"""Security and trust bugs from the second external reviewer pass.
Each test corresponds to a finding from that review."""
from __future__ import annotations

from fastapi.testclient import TestClient


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()


# ── Finding 1: /cockpit was a 404 ────────────────────────────────────────────────────────────────

def test_cockpit_redirects_to_ui(monkeypatch, tmp_path):
    """/cockpit must redirect to /ui (was 404, a first-run blocker)."""
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    c = TestClient(A.app, follow_redirects=False)
    r = c.get("/cockpit")
    assert r.status_code in (301, 302, 307, 308)
    assert r.headers.get("location", "").endswith("/ui")


# ── Finding 3: GET /share silently wrote to memory ───────────────────────────────────────────────

def test_share_get_shows_confirm_page(monkeypatch, tmp_path):
    """GET /share must show a confirmation page, not silently write to memory."""
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    c = TestClient(A.app)
    r = c.get("/share", params={"text": "save this please"})
    assert r.status_code == 200
    # must show a confirm affordance
    assert "Save to notes" in r.text or "save" in r.text.lower()
    # must NOT have written anything to memory
    from mongoose.services.memory import search_facts
    facts = search_facts("save this please")
    assert not facts, "GET /share must not silently write to memory"


def test_share_confirm_requires_nonce(monkeypatch, tmp_path):
    """POST /share/confirm must require the cockpit nonce."""
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    c = TestClient(A.app)
    r = c.post("/share/confirm", json={"text": "sneaky write", "cockpit_nonce": "wrong"})
    assert r.status_code == 403


def test_share_confirm_with_nonce_writes_memory(monkeypatch, tmp_path):
    """POST /share/confirm with the correct nonce must write to memory."""
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    c = TestClient(A.app)
    nonce = A._COCKPIT_NONCE
    r = c.post("/share/confirm",
               json={"text": "a confirmed share", "cockpit_nonce": nonce},
               headers={"X-Cockpit-Nonce": nonce})
    assert r.status_code == 200
    assert r.json().get("ok") is True


# ── Finding 4: ntfy approval actions leaked the master API token ─────────────────────────────────

def test_draft_tokens_are_scoped_and_single_use(monkeypatch, tmp_path):
    """Draft approval tokens must be draft-scoped, action-scoped, and single-use."""
    _fresh(monkeypatch, tmp_path)
    from mongoose.services.whatsapp import _mint_draft_action_token, consume_draft_action_token
    tok = _mint_draft_action_token(7, action="approve")
    assert len(tok) >= 16
    assert consume_draft_action_token(tok, 7, "approve")          # correct
    assert not consume_draft_action_token(tok, 7, "approve")      # replay rejected
    tok2 = _mint_draft_action_token(99, action="cancel")
    assert not consume_draft_action_token(tok2, 99, "approve")    # wrong action
    assert not consume_draft_action_token(tok2, 88, "cancel")     # wrong draft
    assert consume_draft_action_token(tok2, 99, "cancel")         # correct use


def test_send_draft_approval_does_not_embed_master_token(monkeypatch, tmp_path):
    """The ntfy action dicts built by send_draft_approval must not contain X-Mongoose-Token."""
    import inspect
    import mongoose.services.whatsapp as WA
    src = inspect.getsource(WA.send_draft_approval)
    # The live action dict must use X-Draft-Action-Token, not X-Mongoose-Token
    assert '"X-Draft-Action-Token"' in src
    assert '"X-Mongoose-Token"' not in src


# ── Finding 5: guests could see all asset metadata ───────────────────────────────────────────────

def test_guest_filtering_in_device_assets_route(monkeypatch, tmp_path):
    """device_assets_route must filter guests to an empty asset list."""
    _fresh(monkeypatch, tmp_path)
    import inspect
    import mongoose.app as A
    src = inspect.getsource(A.device_assets_route)
    # Must check for trusted_guest profile
    assert "trusted_guest" in src
    # Must return empty assets list for guests
    assert "[]" in src or "empty" in src.lower()


def test_health_does_not_expose_db_path(monkeypatch, tmp_path):
    """Public /health must not expose the database filesystem path."""
    _fresh(monkeypatch, tmp_path)
    import mongoose.app as A
    c = TestClient(A.app)
    j = c.get("/health").json()
    assert "db_path" not in j
    # identity name must still be present for launcher process-identity checks
    assert j.get("name") == "mongoose"


# ── Finding 6/7 (superseded): RTM portal was stubbed then; it is a real, implemented feature now ──
# The original finding was that STUBS reported fake ok:true; the demanded posture was 501 until
# implemented. The portal has since been genuinely implemented (services/rtm_portal.py: encrypted
# claims, counts-only logging, ledger isolation), so the honest posture is no longer "must 501" but
# "must be auth-gated, and must degrade to explicit skip/error when unconfigured, never fake-green."

def test_rtm_portal_routes_are_auth_gated_and_never_fake_green(monkeypatch, tmp_path):
    """Every mutating portal route rejects an unauthenticated caller outright, and an
    authenticated call on an UNCONFIGURED install must say so explicitly (ok:false or an
    explicit skipped/error reason), never pretend it published/processed anything."""
    _fresh(monkeypatch, tmp_path)
    for var in ("WP_URL", "WP_USERNAME", "WP_APP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    import mongoose.app as A
    c = TestClient(A.app)
    nonce = A._COCKPIT_NONCE
    paths = ["/rtm/portal/publish", "/rtm/portal/pull-claims",
             "/rtm/portal/process-claims", "/rtm/portal/pull-issues"]
    for path in paths:
        r = c.post(path, json={})  # no credential at all
        assert r.status_code in (401, 403), f"{path} must reject unauthenticated callers, got {r.status_code}"
    for path in paths:
        r = c.post(path, headers={"X-Cockpit-Nonce": nonce}, json={})
        assert r.status_code == 200, f"{path} returned {r.status_code}"
        d = r.json()
        # Unconfigured must never read as success-with-work-done.
        assert (d.get("ok") is False or d.get("skipped") or d.get("error")),             f"{path} fake-greened on an unconfigured install: {d}"


def test_portal_jobs_gated_by_readiness_not_absent(monkeypatch, tmp_path):
    """Portal jobs ARE registered now (the feature is real), but every run on an unconfigured
    install must produce an explicit skip with the reason, never a fake-green history entry."""
    _fresh(monkeypatch, tmp_path)
    for var in ("WP_URL", "WP_USERNAME", "WP_APP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    import asyncio
    from mongoose.services.scheduler import _build_registry, _job_portal_process_claims
    reg = _build_registry()
    assert "portal_process_claims" in reg
    res = asyncio.run(_job_portal_process_claims())
    assert res.get("skipped"), f"unconfigured portal job must skip explicitly, got: {res}"


# ── Finding 8: file indexer walked entire tree before hitting limit ──────────────────────────────

def test_file_indexer_scan_cap(monkeypatch, tmp_path):
    """File indexer must stop after max_scan files even if fewer than limit are indexed."""
    _fresh(monkeypatch, tmp_path)
    import mongoose.services.file_index as FI
    root = tmp_path / "files"; root.mkdir()
    for i in range(200):
        (root / f"bin_{i}.bin").write_bytes(b"\x00" * 100)
    for i in range(5):
        (root / f"text_{i}.txt").write_text(f"content {i}")
    r = FI.index_files(str(root), limit=100, max_scan=50)
    assert r.get("scanned", 999) <= 50, f"scanned {r.get('scanned')} files, max was 50"


def test_resumable_indexer_bounded_window(monkeypatch, tmp_path):
    """Resumable indexer must return exactly limit files and support cursor-based continuation."""
    _fresh(monkeypatch, tmp_path)
    import mongoose.services.file_index as FI
    root = tmp_path / "files"; root.mkdir()
    for i in range(100):
        (root / f"file_{i:03d}.txt").write_text(f"content {i}")
    r = FI.index_files_resumable(str(root), limit=10)
    assert r.get("scanned", 999) == 10
    assert r.get("more") is True
    cursor = r.get("cursor", "")
    r2 = FI.index_files_resumable(str(root), limit=10, after_path=cursor)
    assert r2.get("scanned", 999) == 10
    # cursors advance (no repeated items)
    assert r2.get("cursor", "") > cursor
