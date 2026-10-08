"""Consolidation Phase 3 guard: the capability answerers must not silently re-fork.

Finding: the audit's "four sources answer what can you do" overstates it at the code level. There is
ONE broad-showcase source (capability_catalog.owner_showcase), consumed identically by the phone path
(app._maybe_capabilities_answer) and by chat_with_memory; capability_index.explain answers the DIFFERENT
"can you do X" question, and capability_guide answers "how do I X". Phase 1 already unified the routing
decision (intent_resolver) they share. This test pins that consistency so a future change cannot drift
the phone path away from the one showcase, re-introducing the divergence M originally hit."""


def test_phone_and_catalog_give_the_same_broad_showcase(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    import mongoose.app as A
    from mongoose.services.capability_catalog import owner_showcase
    assert A._maybe_capabilities_answer("what can you do", is_guest=False) == owner_showcase()


def test_owner_showcase_is_owner_only():
    import mongoose.app as A
    assert A._maybe_capabilities_answer("what can you do", is_guest=True) is None


def test_broad_and_specific_are_distinct_answerers():
    # broad showcase and the live "can you do X" index are different functions answering different
    # questions; they must not be collapsed into one.
    from mongoose.services import capability_catalog, capability_index
    assert capability_catalog.owner_showcase is not getattr(capability_index, "owner_showcase", None)
    assert callable(capability_index.explain)


def test_capability_and_benchmark_routing_is_unified():
    from mongoose.services.intent_resolver import resolve
    assert resolve("what can you do")[0] == "capabilities"
    assert resolve("benchmark results")[0] == "benchmark"
