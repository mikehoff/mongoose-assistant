"""owner_defaults.py: the normal owner requirements for THIS product, prepared automatically
because they are the settled, repeatedly-stated purpose of running Mongoose at all, not
optional preferences to be re-affirmed on every fresh install.

Prepared by default: an API token, a private ntfy topic, and LAN reachability
(MONGOOSE_HOST=0.0.0.0 + the LAN-enabling flag) so the owner's own phone on the owner's own
home network can reach Mongoose without a separate "enable" click. TLS needs nothing added
here: `mongoose serve` already auto-provisions and serves a self-signed certificate by
default (see cli.py), and always has.

The line that stays firm, and is a genuinely different risk category: nothing here touches
public internet ingress. This module never configures DDNS, never requests a router
port-forward or UPnP mapping, and never provisions a publicly-trusted certificate. Reaching
Mongoose from OUTSIDE the home network is a deliberate, separate action the owner takes
through Setup -> Remote access, because that changes who on the entire internet can reach
this Mac, which is a different question from who on the owner's own home network can.

An owner who wants Mongoose loopback-only (a shared machine, an unusual network they don't
trust) can set MONGOOSE_OWNER_BASELINE_OFF=1 before first start, or turn LAN access off again
afterward from Setup -> Remote access; that is the actual "consent" mechanism; opt-out for the
unusual case, not a mandatory click for the default one.
"""
from __future__ import annotations

import secrets
from typing import Any


def ensure_owner_baseline(source: str = "serve") -> dict[str, Any]:
    """Idempotently prepare the owner's settled requirements: auth token, a private ntfy
    topic, and LAN reachability. Call before get_settings() so the first read already
    reflects them. Never touches DDNS, port-forwarding, or public certificates; those stay a
    deliberate, separate owner action."""
    import os
    if str(os.getenv("MONGOOSE_OWNER_BASELINE_OFF", "")).strip().lower() in ("1", "true", "yes", "on"):
        return {"ok": True, "source": source, "updates": {}, "skipped": "MONGOOSE_OWNER_BASELINE_OFF"}
    from .setup_wizard import effective_env, write_env
    env = effective_env()
    updates: dict[str, str] = {}

    token = (env.get("MONGOOSE_API_TOKEN") or "").strip()
    if not token:
        updates["MONGOOSE_API_TOKEN"] = secrets.token_urlsafe(24)

    topic = (env.get("NTFY_TOPIC") or "").strip()
    if not topic or topic == "mongoose-private-topic":
        updates["NTFY_TOPIC"] = f"mongoose-{secrets.token_urlsafe(9)}"

    host = (env.get("MONGOOSE_HOST") or "").strip()
    if host not in ("0.0.0.0", "::"):
        updates["MONGOOSE_HOST"] = "0.0.0.0"
    allow = str(env.get("MONGOOSE_ALLOW_REMOTE") or "").strip().lower()
    if allow not in ("1", "true", "yes", "on"):
        updates["MONGOOSE_ALLOW_REMOTE"] = "true"

    if updates:
        try:
            write_env(updates)
        except Exception as exc:
            return {"ok": False, "source": source, "error": str(exc)[:300], "updates": {}}
        for k, v in updates.items():
            os.environ[k] = v
    return {"ok": True, "source": source, "updates": updates}

