"""Inbound webhook service: receives signed callbacks from example-court.org and other
external sources that know Mongoose's public DDNS URL.

Authentication: HMAC-SHA256 signature in X-Mongoose-Signature header, computed with the
webhook secret from .env (MONGOOSE_WEBHOOK_SECRET). The WordPress plugin embeds this secret
and signs every request so Mongoose can verify it came from the right site.

The endpoint /webhook/ingest accepts POST from example-court.org with events such as:
  - contact_form: a visitor submitted the contact form
  - rtm_query: a leaseholder asked a question via the site
  - plugin_installed / plugin_updated: deployment confirmation
  - admin_note: a manual note from the WP admin interface
  - page_published: a new RTM notice was published

Each event is stored as a notification and surfaced in what_changed.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Any

from .. import profile as _profile


def _webhook_secret() -> str | None:
    return (os.getenv("MONGOOSE_WEBHOOK_SECRET") or "").strip() or None


def generate_webhook_secret() -> str:
    """Generate a fresh webhook secret (URL-safe base64, 32 bytes = 43 chars)."""
    return secrets.token_urlsafe(32)


def verify_signature(body: bytes, signature_header: str) -> bool:
    """Return True if the HMAC-SHA256 signature matches the body using the webhook secret.
    Constant-time comparison to prevent timing attacks."""
    secret = _webhook_secret()
    if not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header or "")


def webhook_configured() -> bool:
    return bool(_webhook_secret())


def public_webhook_url() -> str | None:
    """The full URL that example-court.org should POST events to, or None if DDNS not configured."""
    from ..ddns import public_url
    base = public_url()
    return f"{base}/webhook/ingest" if base else None


def ingest_webhook_event(event: str, source: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Store an inbound webhook event as a notification and trigger any relevant actions.
    Returns {ok, event, stored, action_taken}."""
    site = _profile.site_domain()
    label_map = {
        "contact_form": f"Contact form submission from {site}",
        "rtm_query": f"Leaseholder query from {site}",
        "plugin_installed": f"Plugin installed on {site}",
        "plugin_updated": f"Plugin updated on {site}",
        "deployment_confirmed": f"{site} confirmed its running RTM version",
        "admin_note": f"Admin note from {site}",
        "page_published": f"New RTM notice published on {site}",
        "form_submission": "Form submission from website",
        "payment_received": f"Payment notification from {site}",
    }
    title = label_map.get(event, f"Webhook event: {event}")
    body_parts = []
    for k, v in payload.items():
        if k not in ("signature", "webhook_secret") and v:
            body_parts.append(f"{k}: {v}")
    body = "; ".join(body_parts[:8]) or "(no details)"
    stored = False
    try:
        from .notifications import ingest_notification
        ingest_notification(title=title, body=body, source=source or site,
                            priority="high" if event in ("contact_form", "rtm_query", "payment_received")
                            else "normal",
                            metadata={"event": event, **payload})
        stored = True
    except Exception:
        pass  # notifications store may not be initialised
    # Also record in the brain's activity log
    try:
        from ..db import get_conn
        with get_conn() as con:
            con.execute(
                "INSERT OR IGNORE INTO activity_log (source, event_type, summary, raw_json, created_at) "
                "VALUES (?,?,?,?,datetime('now'))",
                (source or site, event, title,
                 __import__("json").dumps({"event": event, "payload": payload}))
            )
    except Exception:
        pass
    # (d) If the live site is reporting its running RTM version, record it into the shared sync record.
    # This is the live end of the deploy loop: it is what lets the RTM strip confirm the screen, the
    # deploy and example-court.org all agree. Best-effort; never breaks event ingest.
    if event in ("deployment_confirmed", "plugin_installed", "plugin_updated"):
        ver = payload.get("version") or payload.get("pc_rtm_version") or payload.get("rtm_version")
        if ver:
            try:
                from .rtm_deploy_state import record_live_confirmation
                record_live_confirmation(version=str(ver), source=source or site)
            except Exception:
                pass
    return {"ok": True, "event": event, "stored": stored, "title": title}
