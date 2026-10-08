"""Guest delights: curated fun facts (gadgets, animals, chemistry, universe, cars, culture, 1990s
music, good news, AI, Mongoose, the host) with style tags, an entertainment menu, and a
guest chat handler. Everything here is guest-safe: curated, no owner data, no model, no home control."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def test_all_requested_categories_present():
    from mongoose.services.guest_delights import categories
    cats = set(categories())
    for c in ("gadgets", "animals", "chemistry", "universe", "cars", "culture",
              "music90s", "goodnews", "ai", "mongoose", "alex", "tech"):
        assert c in cats, c


def test_random_fact_has_style_tag():
    from mongoose.services.guest_delights import random_fact
    f = random_fact()
    assert f["text"] and f["style"] and f["category"]


def test_category_filter():
    from mongoose.services.guest_delights import random_fact
    f = random_fact(category="mongoose")
    assert f["category"] == "mongoose"


def test_exclude_avoids_repeat():
    from mongoose.services.guest_delights import random_fact
    f1 = random_fact(category="animals", seed="x")
    f2 = random_fact(category="animals", exclude=f1["text"], seed="x")
    assert f1["text"] != f2["text"]


def test_entertain_menu_lists_options():
    from mongoose.services.guest_delights import entertain
    m = entertain("menu")
    assert m["kind"] == "menu"
    labels = [o["label"] for o in m["options"]]
    assert any("fun fact" in l.lower() for l in labels)
    assert m["topics"]


def test_entertain_riddle_has_answer():
    from mongoose.services.guest_delights import entertain
    r = entertain("riddle")
    assert r["question"] and r["answer"]


def test_facts_about_alex_are_light_not_private():
    """Alex facts must be public-spirited, not sensitive. Sanity-check none mention obviously private
    topics."""
    from mongoose.services.guest_delights import _FACTS
    alex = [t for c, s, t in _FACTS if c == "alex"]
    assert alex
    banned = ("password", "address", "medical", "diagnosis", "therapy client", "bank")
    for t in alex:
        low = t.lower()
        assert not any(b in low for b in banned), t


def test_guest_chat_what_can_i_do():
    _fresh()
    from mongoose.app import _maybe_guest_delight
    r = _maybe_guest_delight("what can I do with you")
    assert r and "fun fact" in r.lower()


def test_guest_chat_surprise_me_returns_fact():
    _fresh()
    from mongoose.app import _maybe_guest_delight
    r = _maybe_guest_delight("surprise me")
    assert r and len(r) > 10


def test_non_delight_passes_through():
    _fresh()
    from mongoose.app import _maybe_guest_delight
    assert _maybe_guest_delight("what's the weather tomorrow") is None


def test_delight_endpoint_device_gated():
    _fresh()
    from mongoose.devices import create_pairing_code, claim_pairing_code
    code = create_pairing_code("trusted_guest")
    cs = code["code"] if isinstance(code, dict) else code
    claim = claim_pairing_code(cs, "Sam's iPhone", "phone")
    from fastapi.testclient import TestClient
    from mongoose.app import app
    c = TestClient(app, raise_server_exceptions=False)
    # without token -> rejected
    assert c.get("/device/delight").status_code == 403
    # with token -> a fact
    r = c.get("/device/delight", headers={"x-mongoose-device-token": claim["device_token"]})
    assert r.status_code == 200
    assert r.json().get("text")
