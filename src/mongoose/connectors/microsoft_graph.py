from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import msal

from ..services.calendar_tasks import EventIn, TaskIn, add_event, add_task
from ..services.email_index import EmailIn, ingest_email
from ..settings import get_settings

SCOPES = ["Calendars.Read", "Tasks.Read", "Mail.Read", "offline_access"]
# Minimal scope set for users who ONLY want Microsoft To Do (no mail/calendar). Keeps the consent
# screen to just tasks, so the user isn't asked to grant mailbox access to read a task list.
TODO_ONLY_SCOPES = ["Tasks.Read", "offline_access"]
GRAPH = "https://graph.microsoft.com/v1.0"

# Microsoft's own first-party PUBLIC client (the one "Microsoft Graph PowerShell" uses). It supports
# dynamic delegated consent for scopes like Tasks.Read on PERSONAL Microsoft accounts, with no app
# registration of your own. We fall back to it when the user hasn't registered an Azure app , which is
# the common case now that Microsoft has deprecated registering apps without a directory and gated the
# developer sandbox. The consent screen shows "Microsoft Graph PowerShell" as the app name and an
# unverified notice; that's expected and harmless for reading your own data. A user who wants their own
# app name just sets MS_CLIENT_ID and it takes precedence.
WELL_KNOWN_PUBLIC_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"


def _effective_client_id() -> str:
    settings = get_settings()
    cid = (getattr(settings, "ms_client_id", "") or "").strip()
    return cid or WELL_KNOWN_PUBLIC_CLIENT_ID


def _effective_authority() -> str:
    """Personal Microsoft accounts must use the 'consumers' (or 'common') authority, a tenant GUID
    triggers AADSTS90132 in the device flow. Default to 'consumers' for the well-known public client
    (personal use); honour an explicitly configured tenant otherwise."""
    settings = get_settings()
    tenant = (getattr(settings, "ms_tenant_id", "") or "").strip()
    cid = (getattr(settings, "ms_client_id", "") or "").strip()
    if not cid:
        # using the well-known public client => personal-account path
        return "https://login.microsoftonline.com/consumers"
    # user registered their own app: 'common' default lets both personal + work accounts in
    return f"https://login.microsoftonline.com/{tenant or 'common'}"


def _app_and_cache() -> tuple[msal.PublicClientApplication, msal.SerializableTokenCache]:
    settings = get_settings()
    cache_path = Path(settings.ms_token_cache)
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text(encoding="utf-8"))
    try:
        app = msal.PublicClientApplication(
            _effective_client_id(),
            authority=_effective_authority(),
            token_cache=cache,
            validate_authority=False,
            instance_discovery=False,
        )
    except Exception as exc:
        # MSAL does a network round-trip to the authority at construction. If that fails (no internet,
        # Microsoft outage, restricted network), surface a clean, actionable error rather than a raw
        # ValueError stack trace.
        raise MicrosoftAuthError(
            "Couldn't reach Microsoft sign-in (login.microsoftonline.com). Check the Mac's internet "
            f"connection and try again. Details: {str(exc)[:160]}") from exc
    return app, cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    settings = get_settings()
    path = Path(settings.ms_token_cache)
    path.parent.mkdir(parents=True, exist_ok=True)
    if cache.has_state_changed:
        path.write_text(cache.serialize(), encoding="utf-8")


class MicrosoftAuthError(RuntimeError):
    """Raised when a Microsoft token can't be obtained or has expired/been revoked, i.e. the user
    needs to sign in again. Distinct from a transient network/Graph error so callers (and the UI) can
    say 'sign in again' rather than 'try later'."""


def _token(*, allow_interactive: bool = True, scopes: list[str] | None = None) -> str:
    """Acquire a Graph access token.

    allow_interactive=False is for BACKGROUND callers (the scheduler): it will only use a cached /
    silently-refreshable token and NEVER launch a device-code flow, because a background sync that
    blocks forever on 'enter this code at microsoft.com/devicelogin' is a hang, not a sync. If no
    usable token exists it raises MicrosoftAuthError so the caller can surface 'sign in again'.

    scopes lets a caller request a NARROWER consent (e.g. Tasks.Read only for a To Do-only setup) so the
    user isn't asked to grant mailbox/calendar access just to read tasks. Defaults to the full set."""
    want = list(scopes or SCOPES)
    # MSAL injects offline_access/openid/profile itself and rejects them (and any reserved scopes) when
    # passed to initiate_device_flow/acquire_token_*; strip them so a caller can pass them harmlessly.
    reserved = {"offline_access", "openid", "profile"}
    flow_scopes = [s for s in want if s.lower() not in reserved]
    app, cache = _app_and_cache()
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(flow_scopes, account=accounts[0])
    if not result:
        if not allow_interactive:
            raise MicrosoftAuthError(
                "No usable Microsoft token (not signed in, or the token expired and can't refresh "
                "silently). Sign in again from Setup, or run: mongoose authorize microsoft")
        flow = app.initiate_device_flow(scopes=flow_scopes)
        if "user_code" not in flow:
            raise MicrosoftAuthError(f"Could not start Microsoft sign-in: {flow}")
        print(flow["message"])
        result = app.acquire_token_by_device_flow(flow)
    _save_cache(cache)
    if "access_token" not in result:
        raise MicrosoftAuthError(f"Microsoft token acquisition failed: {result}")
    return result["access_token"]


def _headers(*, allow_interactive: bool = True, scopes: list[str] | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(allow_interactive=allow_interactive, scopes=scopes)}",
            "Accept": "application/json"}


def _todo_summary(synced: dict[str, Any], *, first_run: bool = True) -> str:
    """A warm, human one-liner for a To Do sync result. first_run=True phrases it as the post-sign-in
    moment ("Signed in and pulled..."); first_run=False is for routine re-syncs ("Pulled...")."""
    if not synced.get("ok"):
        if synced.get("needs_signin"):
            return "Couldn't reach your tasks, please sign in again."
        return "Signed in, but the first sync hit a snag; it'll retry automatically." if first_run \
            else "Sync hit a snag; it'll retry automatically."
    count = synced.get("count", 0)
    lists = synced.get("lists_count")
    lead = "Signed in and pulled" if first_run else "Pulled"
    if count == 0:
        return ("Signed in. No open tasks found yet, new ones will appear as you add them." if first_run
                else "No open tasks right now.")
    task_word = "task" if count == 1 else "tasks"
    if lists:
        list_word = "list" if lists == 1 else "lists"
        return f"{lead} {count} {task_word} from {lists} {list_word}."
    return f"{lead} {count} {task_word}."


def begin_device_signin(todo_only: bool = True) -> dict[str, Any]:
    """Start a Microsoft device-code sign-in for the COCKPIT (browser). Returns the user_code +
    verification URL immediately so the cockpit can SHOW them (the plain device flow only prints these
    to the Mac terminal, which a browser user never sees), then completes the flow in a background
    thread and records the result in app_state under 'ms_signin_status'. Poll device_signin_status()."""
    import threading
    from ..db import set_app_state
    want = TODO_ONLY_SCOPES if todo_only else SCOPES
    reserved = {"offline_access", "openid", "profile"}
    flow_scopes = [s for s in want if s.lower() not in reserved]
    try:
        app, cache = _app_and_cache()
        flow = app.initiate_device_flow(scopes=flow_scopes)
    except MicrosoftAuthError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"could not start Microsoft sign-in: {str(exc)[:160]}"}
    if "user_code" not in flow:
        return {"ok": False, "error": f"could not start Microsoft sign-in: {flow}"}

    set_app_state("ms_signin_status", json.dumps({"state": "pending", "user_code": flow.get("user_code"),
                  "verification_uri": flow.get("verification_uri")}))

    def _finish():
        from ..db import set_app_state as _ss
        try:
            result = app.acquire_token_by_device_flow(flow)
            _save_cache(cache)
            if "access_token" not in result:
                _ss("ms_signin_status", json.dumps({"state": "done", "ok": False,
                    "error": result.get("error_description") or result.get("error") or "sign-in failed"}))
                return
            # Delightful first run: pull To Do straight away so the user sees their tasks appear the
            # moment they finish signing in , no separate "Sync now" hunt. Report a friendly summary.
            _ss("ms_signin_status", json.dumps({"state": "syncing", "ok": True,
                "message": "Signed in. Pulling your tasks..."}))
            try:
                synced = sync_microsoft_todo(allow_interactive=False)
                summary = _todo_summary(synced)
                _ss("ms_signin_status", json.dumps({"state": "done", "ok": True,
                    "synced": synced.get("ok", False), "count": synced.get("count", 0),
                    "lists": synced.get("lists_count"), "message": summary}))
            except Exception as exc:  # noqa: BLE001
                # Signed in fine, but the immediate pull hiccuped , still a success, sync can retry.
                _ss("ms_signin_status", json.dumps({"state": "done", "ok": True, "synced": False,
                    "message": f"Signed in. First sync will run shortly ({type(exc).__name__})."}))
        except Exception as exc:  # noqa: BLE001
            _ss("ms_signin_status", json.dumps({"state": "done", "ok": False, "error": str(exc)[:200]}))

    threading.Thread(target=_finish, daemon=True).start()
    return {"ok": True, "user_code": flow.get("user_code"),
            "verification_uri": flow.get("verification_uri"),
            "message": flow.get("message"),
            "expires_in": flow.get("expires_in", 900)}


def device_signin_status() -> dict[str, Any]:
    """Poll the background device-code sign-in started by begin_device_signin()."""
    from ..db import get_app_state
    raw = get_app_state("ms_signin_status", "") or ""
    if not raw:
        return {"state": "idle"}
    try:
        return json.loads(raw)
    except Exception:
        return {"state": "idle"}


def _graph_paged(client: "httpx.Client", url: str, headers: dict[str, str],
                 max_items: int, page_label: str) -> tuple[list[dict], list[str]]:
    """Follow Graph @odata.nextLink pagination up to max_items, collecting .value items. Returns
    (items, warnings). Hardened against Microsoft Graph throttling: a 429 (Too Many Requests) or a
    transient 5xx is retried a few times, honoring the Retry-After header, rather than dropping the rest
    of the collection. A persistent non-2xx is recorded as a warning and stops paging for this
    collection (partial data beats no data); 401/403 raise MicrosoftAuthError (re-sign-in)."""
    import time
    items: list[dict] = []
    warnings: list[str] = []
    next_url: str | None = url
    pages = 0
    while next_url and len(items) < max_items and pages < 50:
        pages += 1
        body = None
        for attempt in range(4):  # 1 try + up to 3 retries
            try:
                resp = client.get(next_url, headers=headers)
                if resp.status_code in (401, 403):
                    raise MicrosoftAuthError(f"Microsoft rejected the request for {page_label} "
                                             f"(HTTP {resp.status_code}), sign in again.")
                if resp.status_code == 429 or resp.status_code in (500, 502, 503, 504):
                    # Throttled or transient server error: honor Retry-After, else exponential backoff,
                    # then retry. Cap the wait so a sync can't hang for minutes.
                    if attempt < 3:
                        ra = resp.headers.get("Retry-After")
                        try:
                            delay = min(float(ra), 30.0) if ra is not None else min(2 ** attempt, 8.0)
                        except (TypeError, ValueError):
                            delay = min(2 ** attempt, 8.0)
                        time.sleep(max(0.0, delay))
                        continue
                    # out of retries
                    warnings.append(f"{page_label}: Microsoft throttled/unavailable (HTTP "
                                    f"{resp.status_code}) after retries, partial data")
                    body = None
                    break
                resp.raise_for_status()
                body = resp.json()
                break
            except MicrosoftAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                if attempt < 3:
                    time.sleep(min(2 ** attempt, 8.0))
                    continue
                warnings.append(f"{page_label}: stopped early ({type(exc).__name__})")
                body = None
                break
        if body is None:
            break
        items.extend(body.get("value", []))
        next_url = body.get("@odata.nextLink")
    return items[:max_items], warnings


def sync_microsoft_calendar(days_ahead: int = 14, max_results: int = 250,
                            allow_interactive: bool = True) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)
    params = {
        "startDateTime": now.isoformat(),
        "endDateTime": end.isoformat(),
        "$top": str(max_results),
        "$orderby": "start/dateTime",
    }
    count = 0
    try:
        headers = _headers(allow_interactive=allow_interactive)
    except MicrosoftAuthError as exc:
        return {"ok": False, "source": "microsoft_graph_calendar", "count": 0,
                "error": "auth", "detail": str(exc), "needs_signin": True}
    with httpx.Client(timeout=20) as client:
        url = f"{GRAPH}/me/calendarView"
        while url:
            response = client.get(url, headers=headers, params=params if "?" not in url else None)
            response.raise_for_status()
            data = response.json()
            for item in data.get("value", []):
                start = item.get("start", {}).get("dateTime")
                end_time = item.get("end", {}).get("dateTime")
                if not start or not end_time:
                    continue
                add_event(EventIn(
                    source="microsoft_graph_calendar",
                    source_event_id=item.get("id"),
                    calendar_name="default",
                    title=item.get("subject") or "Busy",
                    start_at=start,
                    end_at=end_time,
                    busy_status=(item.get("showAs") or "busy").lower(),
                    sensitivity=item.get("sensitivity") or "normal",
                    detail_available=bool(item.get("subject")),
                    raw=item,
                ))
                count += 1
            url = data.get("@odata.nextLink")
            params = None
    return {"ok": True, "source": "microsoft_graph_calendar", "count": count}


def sync_microsoft_todo(max_lists: int = 50, max_tasks_per_list: int = 500,
                        include_completed: bool = False,
                        allow_interactive: bool = True) -> dict[str, Any]:
    """Sync Microsoft To Do task lists into the local task store.

    Hardened: acquires the token ONCE (not per request), follows Graph pagination so large lists
    aren't silently truncated, treats a single failing list as a warning rather than aborting the
    whole sync, distinguishes an auth failure (needs re-sign-in) from a transient error, and skips
    completed tasks by default (they'd otherwise pile up). allow_interactive=False makes it safe to
    call from the scheduler, it will never block on a device-code prompt."""
    count = 0
    skipped_completed = 0
    warnings: list[str] = []
    seen_task_ids: set[str] = set()
    seen_list_names: set[str] = set()
    try:
        # To Do only needs Tasks.Read , request just that so the user isn't asked to grant mailbox or
        # calendar access to read their tasks.
        headers = _headers(allow_interactive=allow_interactive, scopes=TODO_ONLY_SCOPES)
    except MicrosoftAuthError as exc:
        # Not signed in / token dead. Honest, actionable, non-fatal to the rest of the app.
        return {"ok": False, "source": "microsoft_todo", "count": 0,
                "error": "auth", "detail": str(exc), "needs_signin": True}
    try:
        with httpx.Client(timeout=20) as client:
            lists, lw = _graph_paged(client, f"{GRAPH}/me/todo/lists?$top=100",
                                     headers, max_lists, "todo lists")
            warnings.extend(lw)
            for task_list in lists:
                list_id = task_list.get("id")
                list_name = task_list.get("displayName")
                if not list_id:
                    continue
                # Per-list: a deleted/forbidden list must not kill the whole sync.
                try:
                    items, tw = _graph_paged(
                        client, f"{GRAPH}/me/todo/lists/{list_id}/tasks?$top=100",
                        headers, max_tasks_per_list, f"tasks in {list_name or list_id}")
                    warnings.extend(tw)
                except MicrosoftAuthError:
                    raise  # auth death is global, propagate
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"list {list_name or list_id}: skipped ({type(exc).__name__})")
                    continue
                # This list's tasks fetched successfully: it is now authoritative for reconciliation,
                # so a task previously in it but absent now can be safely resolved.
                seen_list_names.add(list_name or "Inbox")
                for item in items:
                    status = item.get("status") or "open"
                    # Record EVERY task id we saw (open or completed) so reconciliation does not mistake
                    # a completed task for a deleted one.
                    if item.get("id"):
                        seen_task_ids.add(item.get("id"))
                    if not include_completed and status == "completed":
                        # Still upsert the completion so the LOCAL row reflects it (otherwise a task you
                        # complete in the app stays 'open' here). It drops out of the open view by status.
                        try:
                            add_task(TaskIn(source="microsoft_todo", source_task_id=item.get("id"),
                                            list_name=list_name, title=item.get("title") or "Untitled task",
                                            status="completed", raw=item))
                        except Exception:  # noqa: BLE001
                            pass
                        skipped_completed += 1
                        continue
                    due = item.get("dueDateTime", {}) or {}
                    reminder = item.get("reminderDateTime", {}) or {}
                    try:
                        add_task(TaskIn(
                            source="microsoft_todo",
                            source_task_id=item.get("id"),
                            list_name=list_name,
                            title=item.get("title") or "Untitled task",
                            due_at=due.get("dateTime"),
                            reminder_at=reminder.get("dateTime"),
                            priority=item.get("importance"),
                            status=status,
                            raw=item,
                        ))
                        count += 1
                    except Exception as exc:  # noqa: BLE001
                        # one malformed task shouldn't drop the rest
                        warnings.append(f"task in {list_name or list_id}: skipped ({type(exc).__name__})")
    except MicrosoftAuthError as exc:
        return {"ok": False, "source": "microsoft_todo", "count": count,
                "error": "auth", "detail": str(exc), "needs_signin": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "source": "microsoft_todo", "count": count,
                "error": "transient", "detail": f"{type(exc).__name__}: {exc}", "warnings": warnings}
    # Reconcile: resolve open tasks that upstream no longer returns (deleted in the To Do app), but only
    # within lists that synced successfully this run, so a transient failure never wipes real tasks.
    from ..services.calendar_tasks import reconcile_todo_tasks
    reaped = reconcile_todo_tasks(seen_task_ids, seen_list_names)
    result: dict[str, Any] = {"ok": True, "source": "microsoft_todo", "count": count,
                              "lists_count": len(lists)}
    if skipped_completed:
        result["skipped_completed"] = skipped_completed
    if reaped.get("resolved"):
        result["resolved_upstream"] = reaped["resolved"]
    if warnings:
        result["warnings"] = warnings
    return result


def _index_ms_attachments(client, message_id: str, headers: dict[str, Any],
                          sender: str | None, subject: str | None,
                          received: str | None, folder: str) -> int:
    """Index attachments on a Graph message. fileAttachment items carry base64
    contentBytes; metadata is always indexed, content for PDF/Word when present."""
    import base64
    from ..services.attachments import index_attachment, _extract_attachment_content
    url = (f"{GRAPH}/me/messages/{message_id}/attachments"
           "?$select=id,name,contentType,size,@odata.type,isInline")
    n = 0
    try:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        atts = resp.json().get("value", [])
    except Exception:
        return 0
    for att in atts:
        if att.get("@odata.type", "").endswith("itemAttachment"):
            continue  # nested message, not a file
        name = att.get("name") or "attachment"
        content_text = None
        # Fetch bytes for content extraction (separate call to get contentBytes).
        try:
            full = client.get(f"{GRAPH}/me/messages/{message_id}/attachments/{att['id']}",
                              headers=headers)
            full.raise_for_status()
            cb = full.json().get("contentBytes")
            if cb:
                content_text = _extract_attachment_content(name, base64.b64decode(cb))
        except Exception:
            content_text = None
        index_attachment(
            source="microsoft_graph_mail",
            source_message_id=message_id,
            filename=name,
            mime_type=att.get("contentType"),
            size_bytes=att.get("size"),
            sender=sender,
            subject=subject,
            email_date=received,
            mailbox=folder,
            privacy="medium",
            content_text=content_text,
        )
        n += 1
    return n


def sync_microsoft_mail(folder: str = "inbox", max_results: int = 100,
                        include_attachments: bool = True) -> dict[str, Any]:
    count = 0
    attachments = 0
    url = f"{GRAPH}/me/mailFolders/{folder}/messages?$top={max_results}&$select=id,subject,from,toRecipients,receivedDateTime,sentDateTime,bodyPreview,body,hasAttachments"
    with httpx.Client(timeout=20) as client:
        # allow_interactive=False: a BACKGROUND sync must NEVER fall into Microsoft's device-code flow
        # (which blocks forever waiting for a human). On an expired/unrefreshable token this raises
        # MicrosoftAuthError, which the scheduler records as "needs sign-in" instead of wedging the whole
        # email sync for 420s. The owner re-authenticates interactively from the Setup/connector card.
        response = client.get(url, headers=_headers(allow_interactive=False))
        response.raise_for_status()
        for item in response.json().get("value", []):
            sender = ((item.get("from") or {}).get("emailAddress") or {}).get("address")
            recipients = ", ".join(
                ((r.get("emailAddress") or {}).get("address") or "")
                for r in item.get("toRecipients", [])
            )
            body = (item.get("body") or {}).get("content") or item.get("bodyPreview") or ""
            subject = item.get("subject") or "(no subject)"
            received = item.get("receivedDateTime")
            ingest_email(EmailIn(
                source="microsoft_graph_mail",
                source_message_id=item.get("id"),
                mailbox=folder,
                sender=sender,
                recipients=recipients,
                subject=subject,
                body=body,
                sent_at=item.get("sentDateTime"),
                received_at=received,
                privacy="medium",
                raw=item,
            ))
            count += 1
            if include_attachments and item.get("hasAttachments"):
                try:
                    attachments += _index_ms_attachments(
                        client, item.get("id"), _headers(), sender, subject, received, folder)
                except Exception:
                    pass
    return {"ok": True, "source": "microsoft_graph_mail", "count": count, "attachments": attachments}
