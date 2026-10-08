"""Optional multilingual embeddings: honest absence, graceful fallback, and the cross-language
same-gist detection they exist for.

The model (paraphrase-multilingual-MiniLM-L12-v2, Apache-2.0, ONNX) is present only if the
owner downloaded it, so these tests simulate it via monkeypatch and assert three things: the
service tells the truth when it is absent, rule-engagement falls back to Jaccard exactly as
before, and with embeddings present a POLISH rewording of an English prompt stands the rule
down, which Jaccard alone provably cannot catch (that contrast is asserted, not assumed).
"""
from __future__ import annotations

import time
import warnings

warnings.filterwarnings("ignore")

EN = "how is the RTM build going?"
PL = "jak idzie budowa RTM?"


def _db(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "e.sqlite3"))
    from mongoose.db import init_db
    init_db()


def test_available_is_honest_when_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    from mongoose.services import embeddings
    a = embeddings.available()
    assert a["ok"] is False and a["files"] is False and a["hint"]


def test_embed_and_similarity_return_none_gracefully(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    import mongoose.services.embeddings as E
    monkeypatch.setattr(E, "_SESSION", None)
    monkeypatch.setattr(E, "_TRIED", False)
    assert E.embed(["anything"]) is None
    assert E.semantic_similarity("a", "b") is None


def test_jaccard_alone_cannot_see_the_polish_rewording():
    """The contrast that justifies the feature: without embeddings, the cross-language
    rewording scores near zero and the rule would fire again."""
    import mongoose.services.rule_engagement as RE
    ta, tb = RE._content_tokens(EN), RE._content_tokens(PL)
    jaccard = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    assert jaccard < 0.6, f"premise broken: jaccard {jaccard}"


def test_cross_language_rewording_stands_the_rule_down_with_embeddings(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    import mongoose.services.rule_engagement as RE
    import mongoose.services.embeddings as E
    # Simulate the model: the two phrasings are near-identical in embedding space (cosine 0.9,
    # comfortably paraphrase territory for this model).
    monkeypatch.setattr(E, "semantic_similarity",
                        lambda a, b: 0.9 if {a, b} == {EN, PL} else 0.2)
    assert RE.rule_ok("rtm_build", EN, "cx1") is True       # card fires once
    v = RE.assess("rtm_build", PL, "cx1")                    # reworded in Polish
    assert v["engage"] is False and "reworded" in v["reason"], v


def test_fallback_to_jaccard_when_embeddings_error(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    import mongoose.services.rule_engagement as RE
    import mongoose.services.embeddings as E
    monkeypatch.setattr(E, "semantic_similarity",
                        lambda a, b: (_ for _ in ()).throw(RuntimeError("model exploded")))
    # Same-language rewording must STILL be caught, via the Jaccard fallback.
    assert RE.rule_ok("rtm_build", "how is the RTM plugin build progressing?", "cf1") is True
    v = RE.assess("rtm_build", "the RTM plugin build, how is it progressing now", "cf1")
    assert v["engage"] is False and "reworded" in v["reason"]


def test_conservative_cosine_mapping(monkeypatch, tmp_path):
    """Cosine 0.8 maps to exactly the 0.6 engagement boundary; mediocre cosine (0.6) maps to
    0.2 and does NOT suppress: only clear paraphrases count."""
    _db(monkeypatch, tmp_path)
    import mongoose.services.rule_engagement as RE
    import mongoose.services.embeddings as E
    monkeypatch.setattr(E, "semantic_similarity", lambda a, b: 0.6)
    assert RE.rule_ok("readiness", "is everything ready?", "cm1") is True
    v = RE.assess("readiness", "different words entirely here", "cm1")
    assert v["engage"] is True, "cosine 0.6 must not count as same gist"
    monkeypatch.setattr(E, "semantic_similarity", lambda a, b: 0.85)
    st = RE._load("cm1"); st["last"]["at"] = time.time(); RE._save("cm1", st)
    v2 = RE.assess("readiness", "different words entirely here", "cm1")
    assert v2["engage"] is False and "reworded" in v2["reason"]
