from __future__ import annotations

from typing import Any

# Model residency probe.
#
# The dual-model architecture (one conversation owner + a silent worker) is only
# viable if BOTH configured resident models actually stay loaded together on this
# machine. On a 24GB base M4 that is a real question, not a spec-sheet one: weights +
# KV cache + Ollama + macOS + Mongoose + Docker/WAHA all compete for unified memory,
# and Ollama may evict one model under pressure.
#
# This probe reports what it can OBSERVE, and says "unknown" otherwise. It never
# infers "yes" from weight sizes. The trustworthy signal is `/api/ps` (what Ollama
# currently holds resident) after both models have been used: that is the runtime
# truth the design notes kept pointing at.


async def loaded_models() -> list[str] | None:
    """Return the model names Ollama currently holds resident (via /api/ps), or None
    if Ollama can't be reached. None means 'unknown', not 'none loaded'."""
    import httpx
    from .settings import get_settings
    base = get_settings().ollama_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{base}/api/ps")
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None
    models = data.get("models") or []
    return [m.get("name") or m.get("model") or "" for m in models]


async def check_residency(owner: str | None = None,
                          worker: str | None = None) -> dict[str, Any]:
    """Report whether the configured owner + worker are CURRENTLY co-resident.

    Returns {owner, worker, pair_resident: 'yes'|'no'|'unknown', loaded, note}.
    - 'yes'     both names are in /api/ps right now.
    - 'no'      Ollama answered but at least one is absent (so they're not staying
                loaded together at this moment).
    - 'unknown' Ollama couldn't be reached, so we genuinely can't say. We do NOT
                guess from weight sizes.
    The honest test is: load both, alternate a few turns, then run this; if it still
    says 'yes', the pair holds on this machine.
    """
    from .settings import get_settings
    s = get_settings()
    owner = owner or s.ollama_conversation_owner
    worker = worker or s.ollama_research_model
    if owner == worker:
        # single-model config: the dual-residency question doesn't apply, and this is
        # knowable without reaching Ollama.
        return {"owner": owner, "worker": worker, "pair_resident": "n/a (same model)",
                "loaded": None,
                "note": "owner and worker are the same model (single-owner default); "
                        "no dual-residency needed. Set OLLAMA_RESEARCH_MODEL to a "
                        "separate worker (e.g. qwen3.5:9b) to use the pair architecture."}
    loaded = await loaded_models()
    if loaded is None:
        return {"owner": owner, "worker": worker, "pair_resident": "unknown",
                "loaded": None,
                "note": "Ollama not reachable; load both models, alternate a few "
                        "turns, then re-run. Residency cannot be inferred from weights."}
    # match on tag prefix so e.g. 'qwen3.5:9b' matches 'qwen3.5:9b-...'
    def present(tag: str) -> bool:
        return any(name == tag or name.startswith(tag) for name in loaded)
    both = present(owner) and present(worker)
    return {"owner": owner, "worker": worker,
            "pair_resident": "yes" if both else "no",
            "loaded": loaded,
            "note": ("both resident now" if both else
                     "not both resident right now; if this persists after alternating "
                     "turns, the pair does not hold on this machine, keep a single owner")}


def format_models_report(r: dict[str, Any]) -> str:
    lines = ["=== Model architecture ==="]
    lines.append(f"  conversation owner (speaks): {r['owner']}")
    lines.append(f"  research worker (silent):    {r['worker']}")
    lines.append(f"  pair co-resident: {r['pair_resident']}")
    if r.get("loaded") is not None:
        lines.append(f"  currently loaded: {', '.join(r['loaded']) or '(none)'}")
    lines.append(f"  {r['note']}")
    return "\n".join(lines)
