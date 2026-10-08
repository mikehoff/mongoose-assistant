"""The capability index: Mongoose's product knowledge as live searchable cards, and the gap
report that answers 'can chat do everything the Cockpit can?' with numbers. Cards are built
at call time from the real registries (TOOLS, routes, CLI parser source, guide workflows), so
a card exists exactly when the capability does: no index to go stale."""
from __future__ import annotations

import asyncio
import warnings

warnings.filterwarnings("ignore")


def test_cards_come_from_all_four_sources():
    from mongoose.services.capability_index import build_cards
    kinds = {c["kind"] for c in build_cards()}
    assert {"chat_tool", "cli", "route", "workflow"} <= kinds
    cards = build_cards()
    assert len(cards) > 400  # 52 tools + ~250 CLI + ~394 routes; shrinking this needs intent


def test_lingo_free_search_finds_the_phone_pairing_path():
    """'the phone QR says connection refused' contains none of the registry's own words
    (pairing, LAN, enable-phone); the synonym bridge must land it anyway."""
    from mongoose.services.capability_index import search
    ids = [c["id"] for c in search("the phone QR says connection refused")]
    assert any("enable-phone" in i or "pairing" in i for i in ids), ids


def test_polish_query_lands_via_aliases():
    from mongoose.services.capability_index import search
    ids = [c["id"] for c in search("wtyczka na stronie")]
    assert any("plugin" in i or "wp_plugin" in i for i in ids), ids


def test_gap_report_shape_and_honesty():
    from mongoose.services.capability_index import gap_report
    g = gap_report()
    assert g["cockpit_routes"] >= g["cockpit_only"] >= 0
    assert g["cli_commands"] >= g["cli_only"] >= 0
    assert g["chat_native"] > 40
    assert "heuristic" in g["note"]  # never oversold


def test_chat_tool_registered_read_only_and_executes():
    from mongoose.tools import TOOLS, READ_TOOLS
    assert "capability_search" in READ_TOOLS
    assert TOOLS["capability_search"]["description"].startswith("Read-only")
    out = asyncio.run(TOOLS["capability_search"]["executor"]({"action": "gaps"}))
    assert out["ok"] and out["gap_report"]["cockpit_routes"] > 0
    out2 = asyncio.run(TOOLS["capability_search"]["executor"](
        {"query": "refresh gmail inbox"}))
    assert out2["ok"] and any("mail" in c["id"] or "gmail" in c["id"]
                              for c in out2["results"]), out2["results"]


def test_search_survives_missing_embeddings_model(monkeypatch):
    """With no embeddings model, search must run on keywords alone, with the signal field saying
    so. Force the no-model path explicitly (embed -> None) so this holds on machines that DO have
    the model installed, not just bare containers , the graceful-degradation contract."""
    import mongoose.services.embeddings as _emb
    monkeypatch.setattr(_emb, "embed", lambda texts: None)
    from mongoose.services.capability_index import search
    res = search("sync my email")
    assert res and all(r["signal"] == "keywords" for r in res)
