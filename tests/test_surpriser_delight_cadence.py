"""Surpriser cadence: delights surface on a shorter gap (20 min) than substantive findings (90 min),
and proactive delight defaults on, so Mongoose feels alive rather than silent — while substantive
findings keep their longer floor so they don't become spam."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    for k in ("MONGOOSE_PROACTIVE_DELIGHT",):
        os.environ.pop(k, None)
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def test_proactive_delight_defaults_on():
    _fresh()
    from mongoose.settings import get_settings
    assert get_settings().proactive_delight_enabled is True


def test_delight_gap_shorter_than_substantive():
    from mongoose.services.surpriser import _MIN_GAP_MINUTES, _DELIGHT_GAP_MINUTES
    assert _DELIGHT_GAP_MINUTES < _MIN_GAP_MINUTES
    assert _DELIGHT_GAP_MINUTES >= 5  # not spammy


def test_delight_only_window_blocks_substantive(monkeypatch):
    """In the window after the delight gap but before the full gap, a non-delight candidate is
    dropped, but a delight may surface."""
    import asyncio
    from datetime import datetime, timezone, timedelta
    _fresh()
    import mongoose.services.surpriser as SUR

    # Land in the DELIGHT-ONLY window: after the short delight gap (5 min) but before the full
    # substantive gap (22 min). 12 minutes ago does that with the current cadence.
    monkeypatch.setattr(SUR, "_last_background_surface_at",
                        lambda con: datetime.now(timezone.utc) - timedelta(minutes=12))

    # candidates: one substantive finding, one delight
    async def fake_cands():
        return [
            {"kind": "link", "text": "substantive", "score": 0.9, "signature": "sig-sub",
             "family": "cross_link", "imminent": False},
            {"kind": "delight", "text": "fun fact", "score": 0.08, "signature": "sig-del",
             "family": "delight", "imminent": False},
        ]
    monkeypatch.setattr(SUR, "candidate_suggestions", fake_cands)
    # neutralise suppression helpers
    monkeypatch.setattr(SUR, "_suppressed_signatures", lambda con: set())
    monkeypatch.setattr(SUR, "_suppressed_families", lambda con: set())
    monkeypatch.setattr(SUR, "_useful_families", lambda con: set())

    res = asyncio.run(SUR.pick_surprise(source="background"))
    # In the delight-only window, the substantive finding is filtered; if anything surfaces it's the
    # delight. The surfaced item is under res["suggestion"].
    if res.get("surfaced"):
        sug = res.get("suggestion", {})
        assert sug.get("family") == "delight" or sug.get("kind") == "delight", \
            f"delight-only window must surface only delights, got {sug.get('family')}/{sug.get('kind')}"


def test_delight_loses_to_substantive_finding(monkeypatch):
    """Score-scale invariant: delights are scored LOW (0-1 scale) so any real finding outranks them.
    Regression for a bug where delight scored 8 on a 0-1 scale and always won, and the 'substantive'
    check used a >=30 threshold that never matched."""
    import asyncio
    _fresh()
    os.environ["MONGOOSE_PROACTIVE_DELIGHT"] = "true"
    import mongoose.settings as SET
    if hasattr(SET, "_settings"): SET._settings = None
    if hasattr(SET.get_settings, "cache_clear"): SET.get_settings.cache_clear()
    import mongoose.services.surpriser as SUR

    async def fake_cands():
        # a real finding (high score) plus a delight (intentionally low). Real one must win.
        from mongoose.services.surpriser import _sig
        return [
            {"kind": "email_meets_calendar", "text": "real finding", "score": 0.9,
             "signature": _sig("link", "real"), "family": "email_meets_calendar", "imminent": False},
            {"kind": "delight", "text": "fun fact", "score": 0.08,
             "signature": _sig("delight", "fun"), "family": "delight", "imminent": False},
        ]
    monkeypatch.setattr(SUR, "candidate_suggestions", fake_cands)
    monkeypatch.setattr(SUR, "_suppressed_signatures", lambda con: set())
    monkeypatch.setattr(SUR, "_suppressed_families", lambda con: set())
    monkeypatch.setattr(SUR, "_useful_families", lambda con: set())
    d = asyncio.run(SUR.pick_surprise(force=True))
    assert d["surfaced"] is True
    assert d["suggestion"]["kind"] != "delight", "a real finding must outrank a delight"
