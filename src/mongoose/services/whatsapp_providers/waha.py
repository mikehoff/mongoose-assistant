from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

from .base import NormalisedWhatsAppMessage

# WAHA provider.
#
# WAHA (devlikeapro/waha) is a self-hosted WhatsApp HTTP API: it runs as a local
# Docker sidecar, sends via REST (POST /api/sendText), reports session state, and
# delivers inbound messages by webhook. Mongoose speaks only HTTP to it; the QR
# linking and WhatsApp Web session live entirely inside the container.
#
# This module translates WAHA's payloads to Mongoose's NormalisedWhatsAppMessage so
# nothing else depends on WAHA's shape.


def _waha_url() -> str:
    return os.getenv("WAHA_URL", "http://127.0.0.1:3000").rstrip("/")


def _api_key() -> str:
    """WAHA rejects every /api/* call with 401 unless the caller presents this key. The
    devlikeapro/waha image now REQUIRES an api key (it ships/generates WAHA_API_KEY), so
    without sending it Connect and QR silently 401 and no QR ever appears. Read it from the
    environment (set WAHA_API_KEY in ~/.mongoose/.env, mirrored in the compose file)."""
    return os.getenv("WAHA_API_KEY", "")


def _headers() -> dict[str, str]:
    key = _api_key()
    return {"X-Api-Key": key} if key else {}


def _session() -> str:
    return os.getenv("WHATSAPP_SESSION", "default")


def _hmac_key() -> str:
    return os.getenv("WHATSAPP_HOOK_HMAC_KEY", "")


def verify_hmac(raw_body: bytes, signature: str | None) -> bool:
    """Verify a WAHA webhook HMAC signature. If no key is configured, verification
    is treated as failed-closed UNLESS the route also accepted a valid
    X-Mongoose-Token (the route enforces that). Never accept arbitrary LAN posts."""
    key = _hmac_key()
    if not key or not signature:
        return False
    digest = hmac.new(key.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    # constant-time compare; WAHA sends hex
    return hmac.compare_digest(digest, signature.strip().lower())


class WahaProvider:
    name = "waha"

    def __init__(self, session: str | None = None):
        # A role-bound session (e.g. "owner" or "mongoose"). Falls back to the legacy single session
        # so existing single-account installs keep working unchanged.
        self._session_name = session

    def _sess(self) -> str:
        return self._session_name or _session()

    def status(self) -> dict[str, Any]:
        """Report session state (NOT just 'container up'). WAHA sessions move
        through states like SCAN_QR_CODE -> WORKING; doctor should surface that."""
        import httpx
        try:
            with httpx.Client(timeout=5, headers=_headers()) as c:
                r = c.get(f"{_waha_url()}/api/sessions")
                data = r.json()
            # find our session
            for s in (data if isinstance(data, list) else []):
                if s.get("name") == self._sess():
                    return {"ok": True, "session": self._sess(),
                            "state": s.get("status") or s.get("state") or "unknown"}
            return {"ok": True, "session": self._sess(), "state": "not_started"}
        except Exception as exc:
            return {"ok": False, "error": f"WAHA unreachable: {exc}", "url": _waha_url()}

    def start_session(self) -> dict[str, Any]:
        """Start (or restart) the WAHA session so it begins authenticating. After this the session
        moves to SCAN_QR_CODE and the QR can be fetched. Idempotent: starting an already-running
        session is fine."""
        import httpx
        try:
            with httpx.Client(timeout=10, headers=_headers()) as c:
                # WAHA: POST /api/sessions/start {name}. Some versions use /api/sessions {name,start}.
                r = c.post(f"{_waha_url()}/api/sessions/start", json={"name": self._sess()})
                if r.status_code in (200, 201):
                    return {"ok": True, "state": "starting"}
                # Already started is not an error for our purposes.
                if r.status_code in (409, 422):
                    return {"ok": True, "state": "already_started"}
                return {"ok": False, "error": f"WAHA start returned {r.status_code}: {r.text[:200]}"}
        except Exception as exc:
            return {"ok": False, "error": f"WAHA unreachable: {exc}", "url": _waha_url()}

    def qr(self) -> dict[str, Any]:
        """Fetch the login QR for the session as a base64 PNG, so the cockpit can render it for the
        user to scan with WhatsApp on their phone. Only meaningful while state is SCAN_QR_CODE."""
        import base64
        import httpx
        try:
            with httpx.Client(timeout=10, headers=_headers()) as c:
                # WAHA returns the QR as an image; request base64 form where supported.
                r = c.get(f"{_waha_url()}/api/{self._sess()}/auth/qr", params={"format": "image"})
                if r.status_code != 200:
                    return {"ok": False, "error": f"QR not available (state may not be SCAN_QR_CODE): {r.status_code}"}
                ctype = r.headers.get("content-type", "")
                if "application/json" in ctype:
                    data = r.json()
                    # some WAHA builds return {"data": "<base64>"} or {"value": "..."}
                    b64 = data.get("data") or data.get("value") or ""
                    return {"ok": True, "qr_base64": b64, "mime": "image/png"}
                # raw image bytes
                b64 = base64.b64encode(r.content).decode("ascii")
                return {"ok": True, "qr_base64": b64, "mime": ctype or "image/png"}
        except Exception as exc:
            return {"ok": False, "error": f"WAHA unreachable: {exc}"}

    def send_text(self, chat_id: str, text: str) -> dict[str, Any]:
        """Send via WAHA REST. This is the ONLY privileged op and is only ever
        called from the approved-outbox path."""
        from ..egress_guard import assert_live_egress
        assert_live_egress("whatsapp_send")   # a test/dev process must never message real contacts
        import httpx
        payload = {"session": self._sess(), "chatId": chat_id, "text": text}
        with httpx.Client(timeout=15, headers=_headers()) as c:
            r = c.post(f"{_waha_url()}/api/sendText", json=payload)
            r.raise_for_status()
            return {"ok": True, "response": r.json() if r.content else {}}

    def fetch_media(self, message_id: str) -> bytes | None:
        import httpx
        try:
            with httpx.Client(timeout=20, headers=_headers()) as c:
                r = c.get(f"{_waha_url()}/api/messages/{message_id}/media")
                if r.status_code == 200:
                    return r.content
        except Exception:
            return None
        return None

    def normalise_webhook(self, payload: dict[str, Any]) -> list[NormalisedWhatsAppMessage]:
        """Translate a WAHA webhook into normalised messages. Handles the 'message'
        and 'message.any' event shapes; session.status events return []."""
        event = payload.get("event", "")
        if event in ("session.status",):
            return []
        body = payload.get("payload", payload) or {}
        # WAHA message payload fields (tolerant to minor shape differences):
        mid = body.get("id") or body.get("messageId") or ""
        chat_id = body.get("from") or body.get("chatId") or ""
        from_me = bool(body.get("fromMe"))
        media = body.get("media") or {}
        has_media = bool(media) or bool(body.get("hasMedia"))
        msg = NormalisedWhatsAppMessage(
            provider="waha",
            session=(payload.get("session")
                     or (body.get("session") if isinstance(body, dict) else None)
                     or (body.get("sessionName") if isinstance(body, dict) else None)
                     or self._sess()),
            provider_message_id=str(mid),
            chat_id=str(chat_id),
            sender_id=str(body.get("participant") or body.get("author") or chat_id),
            sender_name=str(body.get("notifyName") or body.get("pushName") or ""),
            direction="outbound" if from_me else "inbound",
            text=str(body.get("body") or body.get("text") or ""),
            timestamp=str(body.get("timestamp") or ""),
            has_media=has_media,
            media_kind=str(media.get("mimetype", "").split("/")[0] if media else ("media" if has_media else "")),
            media_filename=str(media.get("filename", "") if media else ""),
            media_mime=str(media.get("mimetype", "") if media else ""),
            quoted_message_id=str(body.get("quotedMsgId") or ""),
            privacy="high",
            raw=body,
        )
        if not msg.provider_message_id or not msg.chat_id:
            return []
        return [msg]
