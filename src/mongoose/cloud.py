from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from .db import audit
from .settings import get_settings

# --- Pricing (USD per million tokens) and FX --------------------------------
#
# Verified June 2026. Update if provider pricing changes. Kept here, not in the
# model, so cost is computed deterministically and never trusted to an LLM.
PRICES_USD_PER_MTOK = {
    # Anthropic instant (verified June 2026, $/MTok).
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    # OpenAI instant tiers.
    "gpt-5.4-mini": {"input": 0.75, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "output": 1.25},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    # Google instant tiers (cheapest current escalation targets).
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    # Conservative fallback for an unknown id: assume a relatively expensive
    # instant tier so the budget gate errs toward refusing.
    "_default": {"input": 3.00, "output": 15.00},
}
# USD -> GBP pence. Approximate; the budget has wide headroom so small FX drift
# does not matter. Override with USD_TO_GBP if you want precision.
USD_TO_PENCE = 79.0  # 1 USD ~ 79 pence


def _price(model: str) -> dict[str, float]:
    return PRICES_USD_PER_MTOK.get(model, PRICES_USD_PER_MTOK["_default"])


def estimate_pennies(model: str, input_tokens: int, max_output_tokens: int, usd_to_pence: float = USD_TO_PENCE) -> float:
    """Worst-case cost of one call in pence: full input + full capped output."""
    p = _price(model)
    usd = (input_tokens / 1_000_000) * p["input"] + (max_output_tokens / 1_000_000) * p["output"]
    return usd * usd_to_pence


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate. ~4 chars/token is a standard rough
    rule for English; we round up and add a margin so we never under-estimate
    cost. This intentionally errs high so the budget gate stays conservative.
    """
    if not text:
        return 0
    return int(len(text) / 3.5) + 16  # /3.5 (not /4) and +16 = deliberate over-estimate


@dataclass
class TurnBudget:
    """Tracks cumulative cloud spend across a single user turn."""
    limit_pennies: float
    spent_pennies: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)

    def remaining(self) -> float:
        return self.limit_pennies - self.spent_pennies

    def can_afford(self, est: float) -> bool:
        return (self.spent_pennies + est) <= self.limit_pennies

    def record(self, provider: str, model: str, est: float) -> None:
        self.spent_pennies += est
        self.calls.append({"provider": provider, "model": model, "est_pennies": round(est, 4)})


class BudgetExceeded(Exception):
    pass


async def _call_google(prompt: str, system: str | None, model: str, max_tokens: int) -> str:
    settings = get_settings()
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={settings.google_api_key}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=body)
        r.raise_for_status()
        data = r.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(part.get("text", "") for part in parts).strip()


async def _call_anthropic(prompt: str, system: str | None, model: str, max_tokens: int) -> str:
    settings = get_settings()
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "".join(parts).strip()


async def _call_openai(prompt: str, system: str | None, model: str, max_tokens: int) -> str:
    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()


async def escalate(prompt: str, system: str | None, budget: TurnBudget) -> dict[str, Any]:
    """Try configured instant providers in order, each only if the
    pre-flight cost estimate fits the remaining turn budget. Returns the reply
    and which provider answered, or raises BudgetExceeded if none fits.
    """
    settings = get_settings()
    if not settings.cloud_escalation_enabled:
        raise BudgetExceeded("cloud escalation disabled")
    from .services.egress_guard import egress_allowed
    if not egress_allowed("cloud_llm")[0]:
        raise BudgetExceeded("cloud escalation blocked (non-live context)")

    in_tokens = estimate_tokens((system or "") + prompt)
    max_out = settings.cloud_max_output_tokens

    # Provider registry: name -> (model id, key present, call fn).
    registry = {
        "anthropic": (settings.anthropic_instant_model, settings.anthropic_api_key, _call_anthropic),
        "openai": (settings.openai_instant_model, settings.openai_api_key, _call_openai),
        "google": (settings.google_instant_model, settings.google_api_key, _call_google),
    }
    order = [p.strip() for p in settings.cloud_escalation_order.split(",") if p.strip()]
    chain = []
    for name in order:
        entry = registry.get(name)
        if entry and entry[1]:  # key configured
            chain.append((name, entry[0], entry[2]))

    last_reason = "no_provider_configured"
    for provider, model, fn in chain:
        est = estimate_pennies(model, in_tokens, max_out)
        if not budget.can_afford(est):
            last_reason = f"{provider}_over_budget"
            audit("cloud", "skip_over_budget", False, route=provider, reason=last_reason,
                  payload={"est_pennies": round(est, 4), "remaining": round(budget.remaining(), 4)})
            continue
        try:
            reply = await fn(prompt, system, model, max_out)
        except Exception as exc:
            last_reason = f"{provider}_error"
            audit("cloud", "call_failed", False, route=provider, reason=str(exc))
            continue
        # Charge the worst-case estimate against the budget (conservative: we
        # bill the cap, not the actual, so we can never silently exceed).
        budget.record(provider, model, est)
        audit("cloud", "call", True, route=provider, payload={"model": model, "est_pennies": round(est, 4)})
        return {"provider": provider, "model": model, "reply": reply, "est_pennies": round(est, 4)}

    raise BudgetExceeded(last_reason)
