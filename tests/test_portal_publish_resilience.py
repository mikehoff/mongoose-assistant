"""Publishing to WordPress must survive version skew, and must shout when it doesn't.

Real incident: Mongoose added a new top-level snapshot section ("periods"). WordPress enforces a
strict top-level ALLOWLIST, so an older plugin rejected the WHOLE snapshot with a 400. Publishing is
all-or-nothing, so the leaseholder portal froze on stale data for hours while Mongoose's ledger stayed
correct , a payment marked in the portal looked like it never registered. Nothing alarmed; the owner
noticed a stale percentage. Two failures: no forward compatibility, and no alarm on repeated failure.
"""
import inspect

from mongoose.services import rtm_portal as RP


def test_publish_drops_a_key_the_plugin_does_not_recognise(monkeypatch):
    """A cosmetic new section must never block financial truth reaching the portal."""
    calls = []

    def fake_wp_call(method, path, json_body=None, timeout=None):
        calls.append(dict(json_body or {}))
        if "periods" in (json_body or {}):
            return {"ok": False, "error": ("WordPress returned 400: Snapshot contains an unrecognised "
                                           "top-level section not allowed in the leaseholder portal: periods")}
        return {"ok": True}

    monkeypatch.setattr(RP, "_wp_call", fake_wp_call)
    monkeypatch.setattr(RP, "build_portal_snapshot",
                        lambda full=False: {"ok": True, "flat_count": 18, "flats": [], "periods": {"x": 1}})
    monkeypatch.setattr(RP, "_record_sync_run", lambda *a, **k: None)
    monkeypatch.setattr(RP, "publish_receipt_pdfs", lambda *a, **k: {"ok": True}, raising=False)

    out = RP.publish_portal_snapshot()
    assert out.get("ok") is True, "publish must succeed after dropping the unsupported key"
    # Later calls are separate publishes (e.g. the director summary); only the first two are the
    # snapshot attempt and its retry.
    assert "periods" in calls[0], "the first attempt carries the new section"
    assert "periods" not in calls[1], "the retry drops exactly the key WordPress named"
    assert calls[1]["flats"] == [] and calls[1]["flat_count"] == 18, "real data must survive the retry"


def test_publish_retry_is_bounded(monkeypatch):
    """A server that rejects everything must not spin forever."""
    n = {"c": 0}

    def always_unknown(method, path, json_body=None, timeout=None):
        n["c"] += 1
        return {"ok": False, "error": "WordPress returned 400: unrecognised top-level section not allowed in the leaseholder portal: flats"}

    monkeypatch.setattr(RP, "_wp_call", always_unknown)
    monkeypatch.setattr(RP, "build_portal_snapshot",
                        lambda full=False: {"ok": True, "flat_count": 1, "flats": [], "demands": []})
    monkeypatch.setattr(RP, "_record_sync_run", lambda *a, **k: None)
    RP.publish_portal_snapshot()
    assert n["c"] <= 6, "the drop-and-retry loop must be bounded, not unbounded"


def test_repeated_publish_failure_raises_an_alarm():
    """The guard job must alarm on consecutive publish failures, not only on tampering."""
    from mongoose.services import scheduler as S
    src = inspect.getsource(S._job_portal_snapshot_guard)
    assert "snapshot_publish_health" in src, "the guard must check publish health"
    assert "consecutive_failures" in src
    assert "portal_publish_alarm_sent" in src, "alarm once per outage, not once per interval"


def test_alarm_rearms_after_recovery():
    """A one-shot alarm that never re-arms is a one-time alarm; the next outage would be silent."""
    from mongoose.services import scheduler as S
    src = inspect.getsource(S._job_portal_snapshot_guard)
    assert "fails == 0" in src and 'set_app_state("portal_publish_alarm_sent", "")' in src
