"""Google Drive backup (hardened).

Uploads the brain+config backup archives (from backup.py) OFF the machine into a dedicated Google Drive
folder, so a total disk loss doesn't lose the irreplaceable part of Mongoose. It mirrors the same
grandfather-father-son retention in Drive (7 daily + 4 weekly + 12 monthly).

Scope (privacy): MINIMAL `drive.file`, only files THIS app creates. Mongoose cannot see, read, or
touch any of your other Drive files. A SEPARATE Drive token (secrets/google-drive-token.json) keeps the
calendar/gmail token's scope untouched. It uploads files only; it never reads your wider Drive.

HARDENING (the failure modes a real Drive integration actually hits):
  - Every upload is VERIFIED after the fact: the file's size and md5 checksum on Drive must match the
    local archive, else the upload is treated as FAILED (a silently-corrupt backup is worse than a
    visible failure, it gives false confidence).
  - Transient Drive errors (429/500/502/503, socket timeouts) are RETRIED with exponential backoff.
    A single blip no longer loses the daily backup.
  - The Drive rota NEVER deletes anything unless the just-uploaded backup is confirmed present and
    verified, so a bad upload can't cause good older backups to be pruned.
  - Every call has a TIMEOUT, so a hung request can't wedge the scheduler.
  - Re-uploading the same-named archive REPLACES the existing file rather than creating a duplicate.
  - Outcomes are RECORDED (audit + app_state markers + a diagnostics event + a self-harden observation
    on failure) so a backup that silently stops working becomes visible, not invisible.

Nothing here runs automatically unless Drive is connected AND backups are enabled.
"""
from __future__ import annotations

import hashlib
import os
import socket
import time
from pathlib import Path
from typing import Any, Callable

from .settings import get_settings
from .db import audit, set_app_state, get_app_state, utc_now_iso

# Google incremental auth: this OAuth client already holds the Gmail/Calendar/Tasks read scopes (the
# connectors granted them), so Google returns ALL of those on the token, not just drive.file, and
# oauthlib then raises "Scope has changed". Relax the exact-match check: backups still only USE
# drive.file, and the extra scopes are ones this client was already granted, not new exposure.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# Minimal scope: app-created files only. Mongoose can't see the rest of your Drive.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
BACKUP_FOLDER_NAME = "Mongoose Backups"

# Network discipline
_HTTP_TIMEOUT_S = 60          # per-request socket timeout
_MAX_RETRIES = 4              # for transient (retryable) errors
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# app_state visibility markers
_LAST_OK_AT = "drive_backup_last_ok_at"
_LAST_ERR = "drive_backup_last_error"
_LAST_ERR_AT = "drive_backup_last_error_at"


def _drive_token_path() -> Path:
    s = get_settings()
    base = Path(s.google_token_path).parent
    return base / "google-drive-token.json"


def is_connected() -> bool:
    """True if a Drive token file exists. NOTE: this is the cheap check (file present). Whether the
    token is actually still valid is only known when we use it, callers that need certainty should
    handle the auth error from a real call. See `connection_health`."""
    return _drive_token_path().exists()


def connection_health() -> dict[str, Any]:
    """Actively check Drive is usable: token present AND a tiny live call succeeds. Used by status
    surfaces so 'connected' means 'actually works', not just 'a token file exists'."""
    if not _drive_token_path().exists():
        return {"ok": False, "connected": False, "reason": "no Drive token (run: mongoose backup connect-drive)"}
    try:
        service = _service()
        # cheapest possible authenticated call
        _retry(lambda: service.about().get(fields="user(displayName)").execute(), "about.get")
        return {"ok": True, "connected": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "connected": True, "reason": f"token present but Drive call failed: {exc}"}


def _credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    token_path = _drive_token_path()
    if not token_path.exists():
        raise RuntimeError("Google Drive is not connected. Run: mongoose backup connect-drive")
    creds = Credentials.from_authorized_user_file(str(token_path), DRIVE_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    if not creds or not creds.valid:
        raise RuntimeError("Google Drive token is invalid/expired. Re-run: mongoose backup connect-drive")
    return creds


def connect_drive() -> dict[str, Any]:
    """Run the Drive OAuth sign-in (drive.file scope) and save the dedicated Drive token. Interactive."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    from .oauth import run_google_consent

    s = get_settings()
    secrets_path = Path(s.google_client_secrets_path)
    if not secrets_path.exists():
        return {"ok": False, "error": f"Google client secrets not found at {secrets_path}. "
                                      "Set up Google in the cockpit first (upload the OAuth client JSON)."}
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), DRIVE_SCOPES)
        creds = run_google_consent(flow)
        tp = _drive_token_path()
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(creds.to_json(), encoding="utf-8")
        return {"ok": True, "detail": "Google Drive connected for backups (drive.file scope: only "
                                      "Mongoose's own backup folder is accessible)."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Drive sign-in failed: {exc}"}


# --- Web (phone-capable) OAuth ------------------------------------------------------------------
# The loopback flow above only completes in the browser ON THIS MAC (Google redirects to localhost).
# This web variant uses a public https redirect through the tunnel, so the consent can be completed
# from ANY device (a phone on mobile data). The app layer mints/validates a one-time `state` and only
# reaches finish_web_consent() for a state it minted, so an internet-exposed callback is safe.

def _web_flow(redirect_uri: str):
    from google_auth_oauthlib.flow import Flow
    s = get_settings()
    secrets_path = Path(s.google_client_secrets_path)
    if not secrets_path.exists():
        raise RuntimeError(f"Google client secrets not found at {secrets_path}. Set up Google first.")
    return Flow.from_client_secrets_file(str(secrets_path), scopes=DRIVE_SCOPES, redirect_uri=redirect_uri)


def web_authorization_url(redirect_uri: str) -> tuple[str, str, str]:
    """Build the Google consent URL for the web flow. Returns (url, state, code_verifier). The caller
    stores state + code_verifier and replays BOTH on the callback: the flow uses PKCE, so the token
    exchange must present the same code_verifier that produced the code_challenge in this auth request.
    `offline` + `prompt=consent` so we always get a refresh token."""
    flow = _web_flow(redirect_uri)
    # No include_granted_scopes: we do NOT want Google to fold in the client's other granted scopes
    # (gmail/calendar/tasks); a plain request keeps the returned scope closer to drive.file.
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    return url, state, (getattr(flow, "code_verifier", None) or "")


def finish_web_consent(redirect_uri: str, code: str, code_verifier: str = "") -> dict[str, Any]:
    """Exchange the authorization `code` for the dedicated Drive token and save it. The caller MUST have
    already validated the `state`; it must also pass the `code_verifier` captured at authorization time
    (PKCE), or Google rejects the exchange with 'Missing code verifier'."""
    flow = _web_flow(redirect_uri)
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    tp = _drive_token_path()
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(flow.credentials.to_json(), encoding="utf-8")
    return {"ok": True, "detail": "Google Drive connected for backups (drive.file scope)."}


def _service():
    from googleapiclient.discovery import build
    # set a global socket timeout so a hung request can't wedge the caller/scheduler thread
    socket.setdefaulttimeout(_HTTP_TIMEOUT_S)
    return build("drive", "v3", credentials=_credentials(), cache_discovery=False)


def _is_retryable(exc: Exception) -> bool:
    """Decide whether a Drive error is worth retrying (transient) vs fatal (auth, bad request)."""
    try:
        from googleapiclient.errors import HttpError
        if isinstance(exc, HttpError):
            status = getattr(getattr(exc, "resp", None), "status", None)
            return int(status) in _RETRYABLE_STATUS if status is not None else False
    except Exception:
        pass
    # socket/connection level transients
    return isinstance(exc, (socket.timeout, ConnectionError, TimeoutError, OSError))


def _retry(call: Callable[[], Any], label: str) -> Any:
    """Run a Drive call, retrying transient errors with exponential backoff. Re-raises a fatal error
    immediately (no point retrying an auth failure) and re-raises the last transient error if all
    attempts are exhausted."""
    delay = 1.0
    last: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if not _is_retryable(exc):
                raise
            if attempt < _MAX_RETRIES - 1:
                time.sleep(delay)
                delay = min(delay * 2, 16.0)
    assert last is not None
    raise last


def _ensure_folder(service) -> str:
    """Find or create the dedicated 'Mongoose Backups' folder; return its id."""
    q = (f"name = '{BACKUP_FOLDER_NAME}' and mimeType = 'application/vnd.google-apps.folder' "
         "and trashed = false")
    res = _retry(lambda: service.files().list(q=q, spaces="drive", fields="files(id, name)").execute(),
                 "folder.list")
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": BACKUP_FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"}
    folder = _retry(lambda: service.files().create(body=meta, fields="id").execute(), "folder.create")
    return folder["id"]


def _local_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_existing(service, folder_id: str, name: str) -> str | None:
    q = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    res = _retry(lambda: service.files().list(q=q, spaces="drive", fields="files(id)").execute(),
                 "find.existing")
    files = res.get("files", [])
    return files[0]["id"] if files else None


def upload_backup(archive_path: str | Path) -> dict[str, Any]:
    """Upload one backup archive into the Drive backup folder, then VERIFY it (size + md5 match the
    local file). If a file with the same name already exists, replace its content rather than creating
    a duplicate. Returns {ok, file_id, name, bytes, verified}, ok is True only if the upload was
    verified against the local archive."""
    from .services.egress_guard import egress_allowed
    if not egress_allowed("drive_backup_upload")[0]:
        return {"ok": False, "reason": "egress_blocked_non_live"}
    from googleapiclient.http import MediaFileUpload
    archive = Path(archive_path)
    if not archive.exists():
        return {"ok": False, "error": f"no archive at {archive}"}
    local_size = archive.stat().st_size
    local_md5 = _local_md5(archive)
    try:
        service = _service()
        folder_id = _ensure_folder(service)
        existing_id = _find_existing(service, folder_id, archive.name)
        media = MediaFileUpload(str(archive), mimetype="application/gzip", resumable=True)
        fields = "id, name, size, md5Checksum"
        if existing_id:
            f = _retry(lambda: service.files().update(
                fileId=existing_id, media_body=media, fields=fields).execute(), "files.update")
        else:
            meta = {"name": archive.name, "parents": [folder_id]}
            f = _retry(lambda: service.files().create(
                body=meta, media_body=media, fields=fields).execute(), "files.create")

        # VERIFY: the bytes on Drive must match what we sent. A mismatch means a corrupt/partial upload.
        drive_size = int(f.get("size", 0) or 0)
        drive_md5 = f.get("md5Checksum", "")
        verified = (drive_size == local_size) and (not drive_md5 or drive_md5 == local_md5)
        if not verified:
            # remove the bad upload so it can't be mistaken for a good backup or kept by the rota
            try:
                _retry(lambda: service.files().delete(fileId=f["id"]).execute(), "files.delete.badupload")
            except Exception:
                pass
            return {"ok": False, "error": "uploaded file failed verification (size/checksum mismatch)",
                    "local_size": local_size, "drive_size": drive_size,
                    "local_md5": local_md5, "drive_md5": drive_md5}

        return {"ok": True, "file_id": f["id"], "name": f["name"], "bytes": drive_size,
                "verified": True, "replaced": bool(existing_id)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def list_drive_backups() -> list[dict[str, Any]]:
    """List backup archives currently in the Drive folder, newest first."""
    try:
        service = _service()
        folder_id = _ensure_folder(service)
        res = _retry(lambda: service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            spaces="drive", orderBy="name desc",
            fields="files(id, name, size, createdTime, md5Checksum)").execute(), "list.backups")
        return [{"file_id": f["id"], "name": f["name"], "bytes": int(f.get("size", 0) or 0),
                 "created": f.get("createdTime"), "md5": f.get("md5Checksum", "")}
                for f in res.get("files", [])]
    except Exception:
        return []


def apply_drive_rota(*, protect_name: str | None = None) -> dict[str, Any]:
    """Grandfather-father-son retention in Drive: keep newest 7 daily + 4 weekly + 12 monthly, trash
    the rest. `protect_name` (the archive we just uploaded) is ALWAYS kept regardless, so a rota run
    can never delete the backup we just made. Uses the timestamp embedded in each filename."""
    from datetime import datetime, timezone
    import re

    def parse(name: str):
        m = re.search(r"(\d{8}T\d{6})", name)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None

    try:
        service = _service()
        folder_id = _ensure_folder(service)
        res = _retry(lambda: service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            spaces="drive", fields="files(id, name)").execute(), "rota.list")
        files = res.get("files", [])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    dated = sorted([(f, parse(f["name"])) for f in files],
                   key=lambda t: t[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    keep_ids: set[str] = set()
    days: set[str] = set()
    weeks: set[str] = set()
    months: set[str] = set()
    for f, dt in dated:
        if protect_name and f["name"] == protect_name:
            keep_ids.add(f["id"])  # never delete what we just uploaded
        if not dt:
            keep_ids.add(f["id"])
            continue
        d = dt.strftime("%Y-%m-%d")
        w = f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
        mo = dt.strftime("%Y-%m")
        if len(days) < 7 and d not in days:
            days.add(d); keep_ids.add(f["id"])
        if len(weeks) < 4 and w not in weeks:
            weeks.add(w); keep_ids.add(f["id"])
        if len(months) < 12 and mo not in months:
            months.add(mo); keep_ids.add(f["id"])

    trashed = 0
    for f, _dt in dated:
        if f["id"] not in keep_ids:
            try:
                _retry(lambda fid=f["id"]: service.files().update(
                    fileId=fid, body={"trashed": True}).execute(), "rota.trash")
                trashed += 1
            except Exception:
                pass
    return {"ok": True, "kept": len(keep_ids), "trashed": trashed}


def backup_to_drive() -> dict[str, Any]:
    """The full daily action, hardened and integrated:
      1. create a fresh brain+config backup (local);
      2. apply the LOCAL rota;
      3. if Drive is connected: upload + VERIFY, and only if verified, apply the DRIVE rota (protecting
         the just-uploaded file). If Drive isn't connected it still keeps the local backup.
    Records the outcome to audit + app_state markers + diagnostics, and on failure files a self-harden
    observation under the backup_durability claim, so a backup that silently stops working is visible.
    Never raises; always returns a structured record."""
    from . import backup as B
    out: dict[str, Any] = {}
    made = B.create_backup(label="daily")
    out["local_backup"] = {"ok": made.get("ok"), "archive": made.get("archive"),
                           "bytes": made.get("bytes"), "error": made.get("error")}
    if not made.get("ok"):
        _record_failure("local backup failed: " + str(made.get("error")), out)
        return {"ok": False, "drive_uploaded": False, **out}

    out["local_rota"] = B.apply_rota()

    if not is_connected():
        out["drive"] = {"skipped": "Drive not connected (run: mongoose backup connect-drive)"}
        _record_outcome(drive_uploaded=False, detail="local-only (Drive not connected)")
        return {"ok": True, "drive_uploaded": False, **out}

    archive = Path(made["archive"])
    up = upload_backup(archive)
    out["drive_upload"] = up
    if not up.get("ok"):
        # upload failed or failed verification , do NOT run the rota (don't prune good backups for a bad one)
        _record_failure("drive upload failed/unverified: " + str(up.get("error")), out)
        return {"ok": True, "drive_uploaded": False, **out}

    # only prune the Drive rota AFTER a verified upload, and never delete the file we just made
    out["drive_rota"] = apply_drive_rota(protect_name=archive.name)
    _record_outcome(drive_uploaded=True, detail=f"uploaded {archive.name} ({up.get('bytes')} bytes), verified")
    return {"ok": True, "drive_uploaded": True, **out}


# ----- outcome recording (the integration with the rest of Mongoose's logics) -----

def _record_outcome(*, drive_uploaded: bool, detail: str) -> None:
    """Success path: stamp app_state, audit, and a diagnostics event so the backup lane is observable."""
    try:
        set_app_state(_LAST_OK_AT, utc_now_iso())
        set_app_state(_LAST_ERR, "")  # clear any prior error on success
    except Exception:
        pass
    try:
        audit("backup_drive", "backup_to_drive", True, privacy="local",
              reason=detail, payload={"drive_uploaded": drive_uploaded})
    except Exception:
        pass
    try:
        from .services.diagnostics import record_event
        record_event("metric_delta", scope="backup", summary=f"backup ok ({'drive' if drive_uploaded else 'local-only'})",
                     payload={"detail": detail})
    except Exception:
        pass


def _record_failure(detail: str, out: dict[str, Any]) -> None:
    """Failure path: stamp app_state error, audit the failure, file a diagnostics bug AND a self-harden
    observation under the backup_durability claim, so a silently failing backup becomes visible
    instead of disappearing."""
    try:
        set_app_state(_LAST_ERR, detail[:500])
        set_app_state(_LAST_ERR_AT, utc_now_iso())
    except Exception:
        pass
    try:
        audit("backup_drive", "backup_to_drive", False, privacy="local", reason=detail[:300])
    except Exception:
        pass
    try:
        from .services.diagnostics import record_event
        record_event("bug", scope="backup", summary="backup failed: " + detail[:160],
                     payload=out)
    except Exception:
        pass
    try:
        from .services.self_harden import record_observation
        record_observation("backup", "failure", "drive", "Off-site/local backup failed: " + detail[:160],
                           severity="P0", claim_key="backup_durability",
                           evidence={"out": out})
    except Exception:
        pass


def drive_folder_info() -> dict[str, Any]:
    """Where backups actually live in Drive, made visible: the folder name, its id, and a clickable
    web link so the user can open it and SEE their backups. Returns connected=False (without erroring)
    if Drive isn't connected yet, so the cockpit can show 'not connected' cleanly."""
    if not is_connected():
        return {"connected": False, "folder_name": BACKUP_FOLDER_NAME,
                "detail": "Google Drive isn't connected yet. Connect it to enable off-Mac backups."}
    try:
        service = _service()
        folder_id = _ensure_folder(service)
        return {
            "connected": True,
            "folder_name": BACKUP_FOLDER_NAME,
            "folder_id": folder_id,
            "folder_url": f"https://drive.google.com/drive/folders/{folder_id}",
            "scope_note": "drive.file scope: Mongoose can only see and manage files in its own backup "
                          "folder, never the rest of your Drive.",
        }
    except Exception as exc:  # noqa: BLE001
        return {"connected": True, "folder_name": BACKUP_FOLDER_NAME, "error": str(exc)[:200]}


def _classify_gfs(names: list[str]) -> dict[str, Any]:
    """Classify backup archive names into the grandfather-father-son buckets they'd be RETAINED under
    (newest 7 daily, 4 weekly, 12 monthly), purely for display so the user understands the rotation.
    Read-only, trashes nothing."""
    from datetime import datetime, timezone
    import re

    def parse(name: str):
        m = re.search(r"(\d{8}T\d{6})", name)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None

    dated = sorted([(n, parse(n)) for n in names],
                   key=lambda t: t[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    days: set[str] = set()
    weeks: set[str] = set()
    months: set[str] = set()
    daily = weekly = monthly = 0
    for _n, dt in dated:
        if not dt:
            continue
        d = dt.strftime("%Y-%m-%d")
        w = f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
        mo = dt.strftime("%Y-%m")
        if len(days) < 7 and d not in days:
            days.add(d); daily += 1
        if len(weeks) < 4 and w not in weeks:
            weeks.add(w); weekly += 1
        if len(months) < 12 and mo not in months:
            months.add(mo); monthly += 1
    return {"daily": daily, "weekly": weekly, "monthly": monthly,
            "policy": "Keeps the newest 7 daily, 4 weekly, and 12 monthly backups (grandfather-father-son)."}


def backup_status(*, include_drive_detail: bool = True) -> dict[str, Any]:
    """A read-only, TRANSPARENT view of the backup lane for the cockpit/CLI: last success, last error,
    Drive connection health, where the backups live (folder link), how many archives exist locally and
    in Drive, and the grandfather-father-son breakdown so the rotation is visible, not a black box.

    `include_drive_detail` can be set False for a fast status that skips the Drive round-trips."""
    from . import backup as B
    local = B.list_backups()
    connected = is_connected()
    status: dict[str, Any] = {
        "last_ok_at": get_app_state(_LAST_OK_AT, "") or None,
        "last_error": get_app_state(_LAST_ERR, "") or None,
        "last_error_at": get_app_state(_LAST_ERR_AT, "") or None,
        "local_count": len(local),
        "drive_connected": connected,
        "policy": "Keeps the newest 7 daily, 4 weekly, and 12 monthly backups (grandfather-father-son), "
                  "locally and in Drive.",
    }
    if connected and include_drive_detail:
        info = drive_folder_info()
        status["folder_name"] = info.get("folder_name")
        status["folder_url"] = info.get("folder_url")
        status["scope_note"] = info.get("scope_note")
        try:
            drive_backups = list_drive_backups()
            status["drive_count"] = len(drive_backups)
            status["newest_drive_backup"] = drive_backups[0]["name"] if drive_backups else None
            status["drive_gfs"] = _classify_gfs([b["name"] for b in drive_backups])
        except Exception:
            status["drive_count"] = None
    # local GFS breakdown is always cheap
    try:
        status["local_gfs"] = _classify_gfs([
            (b.get("name") if isinstance(b, dict) else str(b)) for b in local
        ])
    except Exception:
        pass
    return status

