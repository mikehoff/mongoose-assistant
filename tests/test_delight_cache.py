"""Delight cache + autogenerator , scales delights without a model call per pull, with audience
isolation between the owner ('general') well and the guest well."""
from __future__ import annotations

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()


def test_store_dedupes_and_draw_marks_used(db):
    import mongoose.services.delight_cache as DC
    DC._store([
        {"kind": "fact", "audience": "general", "text": "Octopuses have three hearts.", "source": "model"},
        {"kind": "fact", "audience": "general", "text": "octopuses  have   three hearts.", "source": "model"},  # dup (normalised)
    ])
    assert DC.pool_size(audience="general") == 1  # duplicate collapsed by signature
    r = DC.draw(audience="general")
    assert r and r["text"]
    assert DC.pool_size(audience="general", unused_only=True) == 0  # marked used


def test_model_fact_carries_caveat_jokes_do_not(db):
    import mongoose.services.delight_cache as DC
    DC._store([{"kind": "fact", "audience": "general", "text": "A neutron star is dense.", "source": "model"}])
    f = DC.draw(audience="general", kind="fact")
    assert f["caveat"]  # model fact is labelled, not passed off as verified
    DC._store([{"kind": "joke", "audience": "general", "text": "I don't know y.", "source": "model"}])
    j = DC.draw(audience="general", kind="joke")
    assert j["caveat"] is None  # a joke needs no truth caveat


def test_guest_and_owner_wells_are_isolated(db):
    import mongoose.services.delight_cache as DC
    DC._store([{"kind": "fact", "audience": "general", "text": "Owner-well fact one.", "source": "model"}])
    DC._store([{"kind": "fact", "audience": "guest", "text": "Guest-well fact one.", "source": "model"}])
    # drawing from guest must never return the owner-well item and vice versa
    g = DC.draw(audience="guest")
    assert "Guest-well" in g["text"]
    o = DC.draw(audience="general")
    assert "Owner-well" in o["text"]


def test_delight_serves_from_cache_then_falls_back(db):
    import mongoose.services.delight as D, mongoose.services.delight_cache as DC
    # empty cache -> curated fallback (offline)
    r0 = D.delight(allow_model=False)
    assert r0.get("source") == "shipped" and r0.get("text")
    # seed cache -> served from cache
    DC._store([{"kind": "fact", "audience": "general", "text": "Saturn would float.", "source": "model"}])
    r1 = D.delight(kind="fact", allow_model=False)
    assert "Saturn" in r1["text"] and r1["source"] == "model"


def test_topup_is_noop_when_pool_full(db, monkeypatch):
    import mongoose.services.delight_cache as DC
    # Pre-fill above target so top_up shouldn't try to generate.
    monkeypatch.setattr(DC, "_generate_batch", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("should not generate when pool is full")))
    items = [{"kind": "fact", "audience": "general", "text": f"fact number {i}", "source": "model"}
             for i in range(5)]
    DC._store(items)
    out = DC.top_up(audience="general", target_min=3)
    assert out["topped_up"] is False
