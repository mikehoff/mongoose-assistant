"""Reconciliation guards for the capability sources. The catalog (services/capability_catalog) is
the single source of truth for user-facing capabilities; the showcase and the semantic router must
stay consistent with it, and the showcase must never promise a connector that is not available."""
import pytest

from mongoose.services import capability_catalog as cat
from mongoose.services import semantic_router as sr
from mongoose.services.embeddings import embed


def test_every_catalog_intent_is_a_real_router_intent():
    """A capability that declares a routing intent must actually be routable, or the catalog is
    promising a route that does not exist."""
    missing = cat.all_intents() - set(sr._ANCHORS.keys())
    assert not missing, f"catalog intents with no semantic_router anchor: {missing}"


def test_availability_tokens_are_all_known():
    """Every `needs` token must be resolvable by available() and have a user-facing unmet hint, so a
    gated feature can be shown truthfully."""
    known = {None, "home", "mail", "calendar", "voice"}
    for c in cat.CAPABILITIES:
        assert c.get("needs") in known, f"{c['key']} has unknown needs token {c.get('needs')!r}"
        # available() must never raise for a declared token
        assert isinstance(cat.available(c.get("needs")), bool)


def test_showcase_renders_every_area_and_never_falsely_promises():
    lines = cat.showcase_lines()
    assert len(lines) == len(cat.CAPABILITIES)
    for c, line in zip(cat.CAPABILITIES, lines):
        assert c["area"] in line
        # If a needed connector is unavailable, the line must carry the honest "once you connect"
        # nudge rather than stating the feature as if it works now.
        if c.get("needs") and not cat.available(c["needs"]):
            assert "once " in line, f"{c['key']} unavailable but shown as a plain promise: {line}"


def test_no_em_or_en_dashes_in_catalog_copy():
    for c in cat.CAPABILITIES:
        for field in ("area", "blurb"):
            assert "—" not in c[field] and "–" not in c[field], f"dash in {c['key']}.{field}"


@pytest.mark.skipif(embed(["ping"]) is None,
                    reason="local embeddings model not present; router degrades to keyword-only")
def test_catalog_examples_route_to_their_declared_intent():
    """The utterances the catalog advertises for an intent must actually classify to that intent ,
    otherwise the showcase and the router disagree about what a phrase means."""
    sr._ANCHOR_VECS = None  # force a rebuild so merged catalog examples are included
    sr._ANCHORS_UNAVAILABLE = False
    sr._classify_cached.cache_clear()
    for intent in cat.all_intents():
        for ex in cat.examples_for(intent):
            got, score = sr.classify(ex)
            assert got == intent, f"catalog example {ex!r} routed to {got} ({score}), not {intent}"
