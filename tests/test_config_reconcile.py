"""Config-vs-reality reconciliation , the missing installer/updater validation step."""
import mongoose.services.config_reconcile as CR


class _S:
    ollama_chat_model = "gemma4:26b-a4b-it-q4_K_M"
    ollama_tool_model = "gemma4:26b-a4b-it-q4_K_M"
    ollama_code_model = "qwen2.5-coder:14b-instruct-q4_K_M"   # deliberately NOT installed
    ollama_research_model = "gemma4:26b-a4b-it-q4_K_M"
    ollama_keep_alive = "10m"
    protocol_v3_enabled = False
    pi_ssh_host = ""


def _patch(monkeypatch, installed):
    monkeypatch.setattr(CR, "_installed_models", lambda: set(installed))
    monkeypatch.setattr(CR, "get_settings", lambda: _S(), raising=False)
    import mongoose.settings as S
    monkeypatch.setattr(S, "get_settings", lambda: _S())
    # transcriber: pretend ready so it doesn't add noise to these assertions
    import mongoose.services.model_install as MI
    monkeypatch.setattr(MI, "transcriber_status", lambda: {"ready": True})


def test_flags_uninstalled_code_model_as_high(monkeypatch):
    _patch(monkeypatch, installed=["gemma4:26b-a4b-it-q4_K_M"])   # code model absent
    r = CR.reconcile()
    keys = {i["key"]: i for i in r["issues"]}
    assert "model:code" in keys
    assert keys["model:code"]["severity"] == "high"
    assert r["ok"] is False       # a high issue fails the reconcile
    assert "not installed" in keys["model:code"]["detail"].lower()


def test_clean_config_reconciles_ok(monkeypatch):
    class Clean(_S):
        ollama_code_model = "gemma4:26b-a4b-it-q4_K_M"   # now installed
    monkeypatch.setattr(CR, "_installed_models", lambda: {"gemma4:26b-a4b-it-q4_K_M"})
    import mongoose.settings as S
    monkeypatch.setattr(S, "get_settings", lambda: Clean())
    import mongoose.services.model_install as MI
    monkeypatch.setattr(MI, "transcriber_status", lambda: {"ready": True})
    r = CR.reconcile()
    assert not any(i["key"].startswith("model:") for i in r["issues"])
    assert r["ok"] is True


def test_never_judges_when_model_list_unavailable(monkeypatch):
    # if we cannot list installed models, we must NOT flag every role as missing (avoid false alarms)
    monkeypatch.setattr(CR, "_installed_models", lambda: set())
    import mongoose.settings as S
    monkeypatch.setattr(S, "get_settings", lambda: _S())
    import mongoose.services.model_install as MI
    monkeypatch.setattr(MI, "transcriber_status", lambda: {"ready": True})
    r = CR.reconcile()
    assert not any(i["key"].startswith("model:") for i in r["issues"])
