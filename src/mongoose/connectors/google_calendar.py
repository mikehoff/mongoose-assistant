from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from ..oauth import run_google_consent
from googleapiclient.discovery import build

from ..services.calendar_tasks import EventIn, add_event
from ..settings import get_settings

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def _credentials() -> Credentials:
    settings = get_settings()
    token_path = Path(settings.google_token_path)
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


def sync_google_calendar(days_ahead: int = 14, calendar_id: str = "primary", max_results: int = 250) -> dict[str, Any]:
    creds = _credentials()
    service = build("calendar", "v3", credentials=creds)
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)
    response = service.events().list(
        calendarId=calendar_id,
        timeMin=now.isoformat(),
        timeMax=end.isoformat(),
        maxResults=max_results,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    count = 0
    for item in response.get("items", []):
        start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
        end_time = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date")
        if not start or not end_time:
            continue
        add_event(EventIn(
            source="google_calendar",
            source_event_id=item.get("id"),
            calendar_name=calendar_id,
            title=item.get("summary") or "Busy",
            start_at=start,
            end_at=end_time,
            busy_status="busy" if item.get("transparency") != "transparent" else "free",
            sensitivity=item.get("visibility") or "normal",
            detail_available=bool(item.get("summary")),
            raw=item,
        ))
        count += 1
    return {"ok": True, "source": "google_calendar", "count": count}
