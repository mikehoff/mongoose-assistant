import pytest

import mongoose.cloud as cloud
from mongoose.cloud import BudgetExceeded, TurnBudget, estimate_pennies, estimate_tokens, escalate


def test_typical_turn_well_under_2p():
    # 2000 input + 600 capped output on Haiku must be far below the 2p cap.
    est = estimate_pennies("claude-haiku-4-5", 2000, 600)
    assert est < 2.0
    assert est < 0.5
    assert estimate_pennies("gemini-2.5-flash-lite", 2000, 600) < est


def test_openai_nano_pricing_is_cheaper_than_mini():
    assert estimate_pennies("gpt-5.4-nano", 2000, 600) < estimate_pennies("gpt-5.4-mini", 2000, 600)


def test_unknown_model_uses_conservative_default():
    cheap = estimate_pennies("claude-haiku-4-5", 1000, 600)
    unknown = estimate_pennies("some-future-model", 1000, 600)
    assert unknown > cheap  # errs toward refusing


def test_token_estimate_overcounts():
    # Deliberate over-estimate so the gate stays conservative.
    text = "a" * 350  # ~100 tokens at 3.5 chars/token, plus margin
    assert estimate_tokens(text) >= 100


def test_budget_blocks_when_exceeded():
    b = TurnBudget(limit_pennies=2.0)
    assert b.can_afford(1.5)
    b.record("anthropic", "claude-haiku-4-5", 1.5)
    assert not b.can_afford(1.0)  # 1.5 + 1.0 > 2.0
    assert round(b.remaining(), 4) == 0.5


@pytest.mark.asyncio
async def test_escalation_disabled_raises(monkeypatch):
    monkeypatch.setenv("CLOUD_ESCALATION_ENABLED", "false")
    b = TurnBudget(limit_pennies=2.0)
    with pytest.raises(BudgetExceeded):
        await escalate("hello", None, b)


@pytest.mark.asyncio
async def test_escalation_prefers_anthropic_then_openai(monkeypatch):
    monkeypatch.setenv("CLOUD_ESCALATION_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def fake_anthropic(prompt, system, model, max_tokens):
        return "from-haiku"

    async def fake_openai(prompt, system, model, max_tokens):
        return "from-gpt"

    monkeypatch.setattr(cloud, "_call_anthropic", fake_anthropic)
    monkeypatch.setattr(cloud, "_call_openai", fake_openai)

    b = TurnBudget(limit_pennies=2.0)
    out = await escalate("hard question", None, b)
    assert out["provider"] == "anthropic"
    assert out["reply"] == "from-haiku"
    assert b.spent_pennies > 0


@pytest.mark.asyncio
async def test_falls_back_to_openai_when_anthropic_fails(monkeypatch):
    monkeypatch.setenv("CLOUD_ESCALATION_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def boom(prompt, system, model, max_tokens):
        raise RuntimeError("anthropic down")

    async def fake_openai(prompt, system, model, max_tokens):
        return "from-gpt"

    monkeypatch.setattr(cloud, "_call_anthropic", boom)
    monkeypatch.setattr(cloud, "_call_openai", fake_openai)

    b = TurnBudget(limit_pennies=2.0)
    out = await escalate("hard question", None, b)
    assert out["provider"] == "openai"


@pytest.mark.asyncio
async def test_refuses_all_when_budget_too_small(monkeypatch):
    monkeypatch.setenv("CLOUD_ESCALATION_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Tiny budget that no real call can fit.
    b = TurnBudget(limit_pennies=0.0001)
    called = {"n": 0}

    async def should_not_run(prompt, system, model, max_tokens):
        called["n"] += 1
        return "x"

    monkeypatch.setattr(cloud, "_call_anthropic", should_not_run)
    with pytest.raises(BudgetExceeded):
        await escalate("hi", None, b)
    assert called["n"] == 0  # gate refused before calling


@pytest.mark.asyncio
async def test_escalation_order_is_configurable(monkeypatch):
    monkeypatch.setenv("CLOUD_ESCALATION_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setenv("CLOUD_ESCALATION_ORDER", "google,anthropic,openai")

    async def fake_google(prompt, system, model, max_tokens):
        return "from-gemini"

    async def fake_anthropic(prompt, system, model, max_tokens):
        return "from-haiku"

    monkeypatch.setattr(cloud, "_call_google", fake_google)
    monkeypatch.setattr(cloud, "_call_anthropic", fake_anthropic)

    b = TurnBudget(limit_pennies=2.0)
    out = await cloud.escalate("q", None, b)
    assert out["provider"] == "google"  # order put google first


@pytest.mark.asyncio
async def test_default_order_is_claude_first(monkeypatch):
    monkeypatch.setenv("CLOUD_ESCALATION_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.delenv("CLOUD_ESCALATION_ORDER", raising=False)

    async def fake_google(prompt, system, model, max_tokens):
        return "from-gemini"

    async def fake_anthropic(prompt, system, model, max_tokens):
        return "from-haiku"

    monkeypatch.setattr(cloud, "_call_google", fake_google)
    monkeypatch.setattr(cloud, "_call_anthropic", fake_anthropic)

    b = TurnBudget(limit_pennies=2.0)
    out = await cloud.escalate("q", None, b)
    assert out["provider"] == "anthropic"  # default honours Claude-first


@pytest.mark.asyncio
async def test_cloud_provider_order_alias_is_supported(monkeypatch):
    monkeypatch.setenv("CLOUD_ESCALATION_ENABLED", "true")
    monkeypatch.delenv("CLOUD_ESCALATION_ORDER", raising=False)
    monkeypatch.setenv("CLOUD_PROVIDER_ORDER", "google,anthropic,openai")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    async def fake_google(prompt, system, model, max_tokens):
        return "from-gemini"

    async def fake_anthropic(prompt, system, model, max_tokens):
        return "from-haiku"

    monkeypatch.setattr(cloud, "_call_google", fake_google)
    monkeypatch.setattr(cloud, "_call_anthropic", fake_anthropic)

    b = TurnBudget(limit_pennies=2.0)
    out = await cloud.escalate("q", None, b)
    assert out["provider"] == "google"
