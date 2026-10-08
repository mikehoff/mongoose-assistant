"""Self-wiring permissions: when a connector is authorized, immediately wire up and verify the
capabilities it unlocks, rather than leaving them to come alive 'lazily on the next sync'.

Before this, authorizing Google stored a token and returned. The dependent capabilities (email,
calendar) only actually produced data on the next scheduled sync: up to the sync interval later,
so the user authorised, saw nothing, and couldn't tell if it had worked. This closes that gap: a
successful authorize triggers a first sync of each capability the provider unlocks and reports what
came online, so permission → working-capability is one step, observable immediately.

Design:
  - One function, wire_after_authorize(provider), called from every authorize site (CLI, endpoints).
  - It runs a BOUNDED first sync per capability (small window, low cost) so it's safe to run inline.
  - Never raises into the authorize path; a sync that fails is reported, not fatal: the token is
    still saved and the scheduled sync will retry.
"""
from __future__ import annotations

from typing import Any

# provider -> list of (capability_label, callable_returning_summary). The callables are imported
# lazily so this module has no hard dependency on connector libraries.
_PROVIDER_CAPABILITIES: dict[str, list[str]] = {
    "google": ["email", "calendar"],
    "microsoft": ["calendar", "tasks", "email"],
}


def _first_sync(capability: str, provider: str) -> dict[str, Any]:
    """Run a small first sync for one capability under a specific provider. Bounded windows keep
    this cheap enough to run inline right after authorize. Returns {capability, ok, detail}."""
    try:
        if capability == "email" and provider == "google":
            from ..connectors.gmail import sync_gmail
            res = sync_gmail(query="newer_than:7d", max_results=25)
            return {"capability": "email", "ok": True, "detail": _summarise(res)}
        if capability == "email" and provider == "microsoft":
            from ..connectors.microsoft_graph import sync_microsoft_mail
            res = sync_microsoft_mail("inbox", 25)
            return {"capability": "email", "ok": True, "detail": _summarise(res)}
        if capability == "calendar" and provider == "google":
            from ..connectors.google_calendar import sync_google_calendar
            res = sync_google_calendar(days_ahead=14)
            return {"capability": "calendar", "ok": True, "detail": _summarise(res)}
        if capability == "calendar" and provider == "microsoft":
            from ..connectors.microsoft_graph import sync_microsoft_calendar
            res = sync_microsoft_calendar(14)
            return {"capability": "calendar", "ok": True, "detail": _summarise(res)}
        if capability == "tasks" and provider == "microsoft":
            from ..connectors.microsoft_graph import sync_microsoft_todo
            res = sync_microsoft_todo()
            return {"capability": "tasks", "ok": True, "detail": _summarise(res)}
    except Exception as exc:  # noqa: BLE001
        return {"capability": capability, "ok": False, "detail": f"first sync deferred: {exc}"}
    return {"capability": capability, "ok": False, "detail": "no sync path for this capability/provider"}


def _summarise(res: Any) -> str:
    """Best-effort one-line summary of a sync result dict."""
    if isinstance(res, dict):
        for k in ("indexed", "synced", "count", "added", "messages", "events", "items"):
            if k in res:
                return f"{res[k]} {k}"
        if res.get("ok") is not None:
            return "ok" if res["ok"] else "no change"
    return "done"


def wire_after_authorize(provider: str) -> dict[str, Any]:
    """After a connector is authorized, wire up its capabilities NOW: run a first sync of each and
    report what came online. Best-effort; never raises. Connectors for the same capability across
    providers (e.g. calendar) are de-duplicated by running once per capability label.

    Returns {provider, wired: [capability...], results: [...], any_data}.
    """
    provider = (provider or "").lower().strip()
    caps = _PROVIDER_CAPABILITIES.get(provider, [])
    if not caps:
        return {"provider": provider, "wired": [], "results": [],
                "note": "no known capabilities for this provider"}

    results: list[dict[str, Any]] = []
    wired: list[str] = []
    seen: set[str] = set()
    for cap in caps:
        if cap in seen:
            continue
        seen.add(cap)
        r = _first_sync(cap, provider)
        results.append(r)
        if r.get("ok"):
            wired.append(cap)

    # Record the wiring so the cockpit can show 'these capabilities came online when you connected X'.
    try:
        from ..db import audit
        audit("self_wiring", "wire_after_authorize", bool(wired),
              payload={"provider": provider, "wired": wired})
    except Exception:
        pass

    return {"provider": provider, "wired": wired, "results": results, "any_data": bool(wired)}
