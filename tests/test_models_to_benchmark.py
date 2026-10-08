"""models_to_benchmark must reflect the models ACTUALLY installed on this machine, not a
hardcoded candidate list. The old list named models that were never pulled here, so the
overnight benchmark skipped every night with 'nothing to compare' despite plenty of usable
models being present. These tests lock the machine-aware behaviour and the safety cap."""
from types import SimpleNamespace

import mongoose.settings as S
import mongoose.benchmark as B
from mongoose.model_tuning import models_to_benchmark


def _settings(**kw):
    base = dict(ollama_chat_model="gemma4:26b", ollama_tool_model="gemma4:26b",
                ollama_code_model="gemma4:26b", ollama_research_model="gemma4:26b")
    base.update(kw)
    return SimpleNamespace(**base)


def test_reflects_installed_and_excludes_uninstalled_configured(monkeypatch):
    monkeypatch.setattr(S, "get_settings",
                        lambda: _settings(ollama_code_model="missing-coder:14b"))

    async def fake_installed():
        return ["gemma4:26b", "qwen3:14b", "qwen3:30b-a3b", "gpt-oss:20b", "gemma4:12b", "extra:1b"]
    monkeypatch.setattr(B, "installed_models", fake_installed)

    out = models_to_benchmark()
    assert "gemma4:26b" in out              # configured AND installed -> kept as baseline
    assert "missing-coder:14b" not in out   # configured but NOT installed -> dropped
    assert "qwen3:30b-a3b" in out           # installed alternative -> compared
    assert len(out) == 5                    # capped so the nightly run stays bounded
    assert len(set(out)) == len(out)        # de-duplicated


def test_empty_when_fewer_than_two_comparable(monkeypatch):
    monkeypatch.setattr(S, "get_settings", lambda: _settings(
        ollama_chat_model="only:1b", ollama_tool_model="only:1b",
        ollama_code_model="only:1b", ollama_research_model="only:1b"))

    async def fake_installed():
        return ["only:1b"]
    monkeypatch.setattr(B, "installed_models", fake_installed)

    assert models_to_benchmark() == []      # one model is nothing to compare


def test_falls_back_to_configured_when_install_list_unavailable(monkeypatch):
    monkeypatch.setattr(S, "get_settings", lambda: _settings(
        ollama_chat_model="a:1b", ollama_tool_model="b:1b"))

    async def boom():
        raise RuntimeError("ollama down")
    monkeypatch.setattr(B, "installed_models", boom)

    out = models_to_benchmark()
    assert set(out) == {"a:1b", "b:1b", "gemma4:26b"}  # distinct configured, install-check skipped
