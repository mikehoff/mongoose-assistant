from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from ..oauth import run_google_consent
from googleapiclient.discovery import build

from ..services.email_index import EmailIn, ingest_email
from ..settings import get_settings

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _credentials() -> Credentials:
    settings = get_settings()
    # Use the SAME token the Google sign-in writes (settings.google_token_path). A single Google consent
    # grants all requested scopes (Gmail + Calendar + Tasks), so one token serves them all , there is no
    # separate "gmail token" to set up. We still honour a legacy separate gmail-token.json if one already
    # exists (older installs that authorised Gmail on its own), preferring it only when the shared token
    # is absent. This fixes the "Calendar authorised but Gmail says not signed in" split. (audit P5)
    primary = Path(settings.google_token_path)
    legacy_gmail = Path(settings.google_token_path.replace("google-token", "gmail-token"))
    token_path = primary if primary.exists() else (legacy_gmail if legacy_gmail.exists() else primary)
    secrets_path = Path(settings.google_client_secrets_path)
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not secrets_path.exists():
                raise FileNotFoundError(f"Google client secrets not found: {secrets_path}")
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
            creds = run_google_consent(flow)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}


def _body_from_payload(payload: dict[str, Any]) -> str:
    mime = payload.get("mimeType")
    body = payload.get("body", {})
    data = body.get("data")
    if mime == "text/plain" and data:
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")
    parts = payload.get("parts", []) or []
    texts = []
    for part in parts:
        text = _body_from_payload(part)
        if text:
            texts.append(text)
    return "\n".join(texts)


def _walk_attachment_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Find attachment parts in a Gmail payload tree. Each returned dict carries
    filename, mimeType, size, and the attachmentId needed to fetch the bytes."""
    found: list[dict[str, Any]] = []
    filename = payload.get("filename") or ""
    body = payload.get("body", {}) or {}
    if filename and body.get("attachmentId"):
        found.append({
            "filename": filename,
            "mime_type": payload.get("mimeType"),
            "size_bytes": body.get("size"),
            "attachment_id": body["attachmentId"],
        })
    for part in payload.get("parts", []) or []:
        found.extend(_walk_attachment_parts(part))
    return found


def _index_gmail_attachments(service, message: dict[str, Any], headers: dict[str, str],
                             save: bool = False) -> int:
    """Index every attachment on a Gmail message: metadata always, and extracted
    text for PDF/Word when the bytes can be fetched. Best-effort per attachment."""
    import base64
    from ..services.attachments import index_attachment, _extract_attachment_content
    payload = message.get("payload", {})
    msg_id = message.get("id")
    n = 0
    for att in _walk_attachment_parts(payload):
        content_text = None
        try:
            data = service.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=att["attachment_id"]).execute()
            raw = base64.urlsafe_b64decode(data.get("data", "").encode("utf-8")) if data.get("data") else b""
            if raw:
                content_text = _extract_attachment_content(att["filename"], raw)
        except Exception:
            content_text = None  # metadata still indexed below
        index_attachment(
            source="gmail_api",
            source_message_id=msg_id,
            filename=att["filename"],
            mime_type=att.get("mime_type"),
            size_bytes=att.get("size_bytes"),
            sender=headers.get("from"),
            subject=headers.get("subject") or "(no subject)",
            email_date=headers.get("date"),
            mailbox=",".join(message.get("labelIds", [])),
            privacy="medium",
            content_text=content_text,
        )
        n += 1
    return n


def sync_gmail(query: str = "newer_than:30d", max_results: int = 100,
               include_attachments: bool = True) -> dict[str, Any]:
    creds = _credentials()
    service = build("gmail", "v1", credentials=creds)
    listed = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    count = 0
    attachments = 0
    for item in listed.get("messages", []):
        message = service.users().messages().get(userId="me", id=item["id"], format="full").execute()
        payload = message.get("payload", {})
        headers = _headers(payload)
        ingest_email(EmailIn(
            source="gmail_api",
            source_message_id=message.get("id"),
            mailbox=",".join(message.get("labelIds", [])),
            sender=headers.get("from"),
            recipients=headers.get("to"),
            subject=headers.get("subject") or "(no subject)",
            body=_body_from_payload(payload) or message.get("snippet", ""),
            sent_at=headers.get("date"),
            received_at=headers.get("date"),
            privacy="medium",
            raw={"threadId": message.get("threadId"), "labelIds": message.get("labelIds", [])},
        ))
        count += 1
        if include_attachments:
            try:
                attachments += _index_gmail_attachments(service, message, headers)
            except Exception:
                pass  # attachment indexing must never break the mail sync
    return {"ok": True, "source": "gmail_api", "count": count, "attachments": attachments}
