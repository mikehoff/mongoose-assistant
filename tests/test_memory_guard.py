"""Memory guard , keep the pinned main model, evict only secondary models, surface external hogs."""
import mongoose.services.memory_guard as MG


def test_snapshot_protects_chat_model(monkeypatch):
    monkeypatch.setattr(MG, "_resident_models", lambda: [{"name": "gemma4:26b-a4b-it-q4_K_M"}])
    monkeypatch.setattr(MG, "_external_hogs", lambda: [])

    class S:
        ollama_chat_model = "gemma4:26b-a4b-it-q4_K_M"
        ollama_base_url = "http://127.0.0.1:11434"
    monkeypatch.setattr(MG, "get_settings", lambda: S())
    snap = MG.snapshot()
    assert snap["protected_model"] == "gemma4:26b-a4b-it-q4_K_M"
    assert snap["protected_resident"] is True
    assert snap["secondary_models_resident"] == []   # nothing wasteful


def test_apply_safe_relief_evicts_only_secondary(monkeypatch):
    # chat model + a stray second model both resident
    monkeypatch.setattr(MG, "_resident_models",
                        lambda: [{"name": "gemma4:26b-a4b-it-q4_K_M"}, {"name": "qwen3:14b"}])
    monkeypatch.setattr(MG, "_external_hogs", lambda: [])

    class S:
        ollama_chat_model = "gemma4:26b-a4b-it-q4_K_M"
        ollama_base_url = "http://127.0.0.1:11434"
    monkeypatch.setattr(MG, "get_settings", lambda: S())
    evicted = []
    monkeypatch.setattr(MG, "_evict_model", lambda m: (evicted.append(m) or True))
    # force the "high pressure" path irrelevant here (apply_safe_relief always safe to call)
    res = MG.apply_safe_relief()
    assert res["ok"] is True
    assert res["evicted_secondary_models"] == ["qwen3:14b"]     # secondary gone
    assert "gemma4:26b-a4b-it-q4_K_M" not in evicted             # primary NEVER evicted


def test_external_hogs_carry_reclaim_actions(monkeypatch):
    # Chrome over threshold -> a hog with an actionable reclaim string, never a server-side kill
    monkeypatch.setattr(MG, "_ps_rss_mb",
                        lambda name: 1200.0 if "Chrome" in name else 0.0)
    hogs = MG._external_hogs()
    chrome = [h for h in hogs if "Chrome" in h["name"]]
    assert chrome and chrome[0]["safe_server_action"] is False
    assert "restore" in chrome[0]["reclaim"].lower() or "quit" in chrome[0]["reclaim"].lower()
