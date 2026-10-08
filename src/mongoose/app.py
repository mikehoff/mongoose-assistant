from __future__ import annotations

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from . import __version__
from .db import init_db
from .llm import USER_REQUESTABLE_ROLES, ask_ollama, chat_with_memory
from .persona import persona_payload, persona_card
from .role_router import classify_role
from .tools import run_tool_turn, tool_catalogue
from .cloud import BudgetExceeded, TurnBudget, escalate
from .doctor import run_doctor, system_status
from .services.calendar_tasks import EventIn, TaskIn, add_event, add_task, availability, events_today, tasks_today
from .services.home_router import HomeCommand, list_route_preferences, route_home_command
from .services.notifications import NotificationIn, ingest_notification, search_notifications
from .services.weather import weather_summary
from .settings import get_settings

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Brain wake-up self-check: guarantee the brain comes back with FULL working memory after a
    # restart or reinstall: checkpoint a stranded WAL, materialise any missing table, and rebuild
    # any desynced FTS index so memory SEARCH actually works (not just "the DB opens"). Cheap when
    # healthy; never blocks boot. The result is stashed for the cockpit/CLI to surface.
    try:
        from .brain_wakeup import wake_up
        app.state.brain_wakeup = wake_up(repair=True)
    except Exception as _wexc:
        app.state.brain_wakeup = {"ok": False, "problems": [f"wake-up check errored: {_wexc}"]}
    # Fuzzy-matching model: make it just BE there. If files are missing, fetch them in a
    # daemon background thread (quiet, ~500MB once, atomic .part rename, disable with
    # MONGOOSE_AUTO_EMBEDDINGS=0). Boot never waits; search upgrades itself from keywords to
    # hybrid the moment the files land.
    try:
        from .services.embeddings import available as _emb_available, ensure_model as _emb_ensure
        if not _emb_available().get("ok"):
            import threading as _emb_th
            _emb_th.Thread(target=lambda: _emb_ensure(quiet=True), daemon=True).start()
    except Exception:
        pass
    # Create the ONE permanent conversation at startup so the Chat tab opens onto an existing resident
    # thread (with whatever context is already there) rather than a fresh disposable session, and so
    # voice/phone/terminal/HA all attach to the same thread from the first message.
    try:
        from .services.memory import ensure_conversation, DEFAULT_CONVERSATION_ID
        ensure_conversation(DEFAULT_CONVERSATION_ID, title="Mongoose")
    except Exception:
        pass
    # Pre-warm the expensive read-only status reports in the background so the FIRST cockpit load is
    # already fast (SWR then keeps them warm). Daemon thread, delayed so it never competes with model
    # or embeddings load at boot; boot never waits and any failure is silent.
    try:
        import threading as _pw_th

        def _status_prewarm():
            import time as _pw_t
            import threading as _pw_t2
            _pw_t.sleep(4)  # let the model/embeddings settle before spending CPU on reports

            def _warm_readiness():
                try:
                    from .services.readiness import readiness_report
                    _status_cached("readiness", 45.0, readiness_report)
                except Exception:
                    pass

            def _warm_sysdoc():
                try:
                    from .doctor import system_status as _ss, run_doctor as _rd
                    _status_cached("system_status", 20.0, _ss)
                    _status_cached("doctor", 30.0, _rd)
                except Exception:
                    pass
                # Config-vs-reality reconciliation: log any config drift at boot so a role pointing at an
                # uninstalled model (or a feature enabled without its dependency) is LOUD, not silent.
                try:
                    from .services.config_reconcile import reconcile as _reconcile
                    import logging as _rlog
                    _rc = _reconcile()
                    app.state.config_reconcile = _rc
                    for _iss in _rc.get("issues", []):
                        if _iss.get("severity") == "high":
                            _rlog.getLogger("mongoose").warning(
                                "config drift: %s, fix: %s", _iss.get("detail"), _iss.get("fix"))
                except Exception:
                    pass

            def _warm_rtm():
                try:
                    from .services import rtm_management as _M
                    _status_cached("rtm_dashboard", 30.0, _M.management_dashboard)
                except Exception:
                    pass

            def _warm_device_summary():
                # Prewarm each paired phone's Status card so the FIRST /device/summary after a restart is
                # instant instead of paying the ~1.8s cold compute (the thing that felt slow post-deploy).
                try:
                    import asyncio as _pw_aio
                    import time as _pw_time
                    from .devices import list_devices, device_whoami
                    for _d in (list_devices() or []):
                        if _d.get("profile") != "primary_phone":
                            continue
                        _did = str(_d.get("device_id") or "")
                        if not _did:
                            continue
                        _state = device_whoami(_did) or _d
                        _payload = _pw_aio.run(_build_owner_summary(_state, budget=1.8))
                        _SUMMARY_CACHE[_did] = (_pw_time.monotonic(), _payload)
                except Exception:
                    pass
            def _warm_liveness():
                try:
                    from .liveness import liveness as _lv
                    _status_cached("liveness", 30.0, _lv)
                except Exception:
                    pass

            def _warm_tuning():
                try:
                    from .model_tuning import evaluate as _ev
                    _status_cached("tuning", 60.0, lambda: _ev(allow_auto_apply=False))
                except Exception:
                    pass

            def _warm_daily():
                try:
                    import asyncio as _pw_aio2
                    from .services.daily_picture import daily_operating_picture as _dop
                    _status_cached("daily", 180.0, lambda: _pw_aio2.run(_dop()))
                except Exception:
                    pass

            def _warm_release_check():
                try:
                    import asyncio as _pw_aio3
                    from .release_check import release_check as _rc
                    _pw_aio3.run(_status_cached_async("release_check", 120.0, _rc))
                except Exception:
                    pass
            # Parallel so all panels warm within ~one readiness compute, not the sum of them.
            for _f in (_warm_readiness, _warm_sysdoc, _warm_rtm, _warm_device_summary,
                       _warm_liveness, _warm_tuning, _warm_daily, _warm_release_check):
                _pw_t2.Thread(target=_f, daemon=True).start()
        _pw_th.Thread(target=_status_prewarm, daemon=True).start()
    except Exception:
        pass
    # Start the background scheduler in-process so recurring jobs (daily brief, email sync, file
    # reindex, DDNS update) actually run whenever the server is up, no separate `mongoose service`
    # / launchd step required for the common case. run_due_jobs is time-based and restart-safe, so if
    # an external scheduler (launchd) is ALSO running, jobs won't double-fire (whoever runs first
    # updates last_run). Opt out with MONGOOSE_INPROCESS_SCHEDULER=false if you manage it externally.
    import os as _os
    _sched_task = None
    _watchdog = None
    _run_loops = str(_os.getenv("MONGOOSE_INPROCESS_SCHEDULER", "true")).lower() not in ("0", "false", "no")
    if _run_loops:
        try:
            import asyncio as _asyncio
            from .services.scheduler import run_scheduler
            _sched_task = _asyncio.create_task(run_scheduler())
        except Exception:
            _sched_task = None
        # Runtime watchdog: every ~7s, check the scheduler task is alive and RESTART it if it died,
        # and record loop lag + a heartbeat. Honest about its limit (can't run if the whole loop is
        # wedged, the browser-side watchdog covers that), but it repairs the silent-task-death case.
        # Off by default in tests (they shouldn't start indefinite loops); on for the real server.
        if str(_os.getenv("MONGOOSE_RUNTIME_WATCHDOG", "true")).lower() not in ("0", "false", "no"):
            try:
                from .services.runtime_watchdog import RuntimeWatchdog
                from .services.scheduler import run_scheduler as _rs
                _watchdog = RuntimeWatchdog()
                _watchdog.supervise("scheduler", lambda: _rs(), _sched_task)
                _watchdog.start()
            except Exception:
                _watchdog = None
    try:
        yield
    finally:
        if _watchdog is not None:
            try:
                await _watchdog.stop()
            except BaseException:
                pass
        if _sched_task is not None:
            _sched_task.cancel()
            try:
                # Wait briefly for the task to unwind, but never block shutdown indefinitely: if the
                # tick is mid synchronous work (a sync DB call can't be interrupted by cancel()), give
                # up after a short window rather than deadlocking the event loop on exit.
                import asyncio as _asyncio
                await _asyncio.wait_for(_asyncio.shield(_sched_task), timeout=2.0)
            except BaseException:
                pass


app = FastAPI(title="Mongoose", version=__version__, lifespan=lifespan)


@app.exception_handler(Exception)
async def _graceful_500_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort safety net: an UNEXPECTED error in any endpoint returns a structured, honest
    degraded response instead of an opaque framework 500. The request id (from the observability
    middleware) is echoed so the matching server-side traceback is findable. This keeps one bad
    subsystem from blanking the cockpit: the browser-side loaders catch a clean JSON error and
    show a 'temporarily unavailable / retry' card rather than a dead page.

    HTTPException (deliberate 4xx like nonce/auth failures) is NOT handled here: FastAPI's own
    handler keeps those exact status codes; this only catches genuinely-unexpected exceptions.
    """
    rid = getattr(request.state, "request_id", "") if hasattr(request, "state") else ""
    return JSONResponse(
        status_code=503,  # 503 (not 500): "temporarily unavailable", the honest signal to retry
        content={
            "ok": False,
            "error": "temporarily_unavailable",
            "message": "Mongoose hit an unexpected error handling this request. It's usually "
                       "transient; retry in a moment.",
            "request_id": rid,
            "path": _safe_path_for_log(request.url.path),
        },
    )


# Routes that can write config, control the home, index/expose private content,
# start OAuth, or mutate memory. These are refused for non-loopback clients
# unless the operator explicitly opts in with MONGOOSE_ALLOW_REMOTE=true AND a
# matching MONGOOSE_API_TOKEN. The default (localhost only) needs no token.
# Security model: ALL routes are localhost-only by default. Only paths in
# _PUBLIC_PREFIXES are reachable from non-loopback clients without a token. This
# is an allowlist, not a blocklist, so a newly added route is private by default
# (the old blocklist let private read routes leak when someone forgot to list
# them). Remote access to private routes still requires MONGOOSE_ALLOW_REMOTE +
# token.
_PUBLIC_PREFIXES = (
    # Safe public front door: contains no private data, just links to local setup/cockpit/phone surfaces.
    "/",
    "/health",
    # Externalised cockpit CSS/JS (content-hashed, immutable). Non-sensitive UI code, same class as
    # /static; must load before auth on the cockpit shell.
    "/ui-asset",
    # The WhatsApp webhook is reached by the WAHA container from a non-loopback
    # address, so the global token guard would 403 it before its own verifier runs.
    # It is allowlisted here ONLY because the route itself enforces HMAC (or token)
    # verification and rejects anything unverified with 401. Do not add other routes
    # here without an equivalent in-route auth check.
    "/whatsapp/webhook",
    # The WordPress webhook (example.org over the Cloudflare tunnel) is reached from a non-loopback
    # address, so the global token guard would 403 it before its own verifier runs. Allowlisted ONLY
    # because the route itself enforces an HMAC-SHA256 signature over the body (verify_signature) and
    # rejects anything unsigned/mismatched with 401. This is how a director's portal click can nudge
    # Mongoose to pull its queue. Same standard as /whatsapp/webhook: in-route HMAC auth is mandatory.
    "/webhook/ingest",
    # MCP server for the Claude.ai custom connector (reached over the Cloudflare tunnel). Allowlisted ONLY
    # because the route enforces its own Bearer-token check (MONGOOSE_MCP_TOKEN) and 401s anything
    # unauthenticated; the global X-Mongoose-Token guard would otherwise reject Claude's Authorization
    # header. Read-only tools only. Same standard as /webhook/ingest: mandatory in-route auth.
    "/mcp",
    # OAuth for the Claude.ai custom connector. Claude's form only offers OAuth (Client ID/Secret), no
    # static header, so Mongoose runs a minimal single-owner authorization server. These endpoints are
    # public by design (that IS the OAuth surface); each does its own validation (client match, PKCE,
    # claude.ai redirect_uri), and /mcp still requires a valid Bearer access token minted by /token.
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/authorize",
    "/token",
    "/register",
    # Bundled static assets (fonts) must load on the login page BEFORE auth, and they
    # are non-sensitive, read-only, and served from a fixed directory with a traversal
    # guard (see /static/fonts/{name}). Safe to allowlist.
    "/static",
    # Device pairing: /pair is the PWA pairing landing page a phone opens after scanning
    # the QR, and /pair/claim is where the device redeems its one-time code. Both must be
    # reachable pre-auth (the device has no credential yet). Safe because /pair/claim
    # requires a valid, unexpired, single-use code that only an AUTHENTICATED user could
    # have minted (see devices.create_pairing_code, which is nonce/session-gated).
    "/pair",
    # Guest URL invites: owner-created HTTPS/DDNS links that create a scoped trusted_guest
    # device after the guest completes the delightful onboarding form. Safe pre-auth because
    # the invite token is high-entropy, hashed at rest, single-use, and expiry-limited.
    "/guest/invite",
    # Pairing diagnostic/preflight: the device being paired must be able to reach this to
    # troubleshoot, so it has to be public like /pair itself. Privacy-safe (no secrets).
    "/pairing/preflight",
    # Credential recovery: a device whose token was cleared/lost has nothing to present, so the
    # username+password login that mints a fresh token must be reachable pre-auth. Safe because the
    # route itself verifies credentials and is per-IP rate limited; the profile is set server-side.
    "/device/login",
    # Public static shell for a paired device's PWA. It contains no private data;
    # JS calls /device/* with the device token held on that device.
    "/device/launchpad",
    "/device/cockpit",
    # Data-dump downloads via a shareable link. The /dump/{id}/download route is allowlisted ONLY
    # because it self-validates a high-entropy download token that is hashed at rest, expiry-limited,
    # and constant-time compared (see data_dump.validate_download_token), refusing anything unverified
    # with 403. When no token is supplied it falls back to requiring the cockpit nonce. This is the same
    # safety standard as /guest/invite. Do NOT add mutating routes under /dump without an equivalent
    # in-route auth check (dump CREATE/redeem/list live under /cockpit/dump and stay nonce-gated).
    "/dump",
    # Google Drive backup OAuth callback (reached by the user's phone browser via the tunnel after Google
    # consent). Allowlisted ONLY because /oauth/drive/callback self-validates a one-time, high-entropy
    # `state` that is minted exclusively by the nonce-gated /cockpit/drive-connect-url and expires in 10
    # minutes; an unknown/expired state is refused. Same standard as /pair/claim. GET only. The token
    # exchange also requires a Google-issued code for THIS client, which cannot be forged.
    "/oauth/drive",
    # Generated RTM document downloads via a capability link (chat message / cockpit button). Same
    # standard as /dump and /guest/invite: /rtm/doc/{token} self-validates a high-entropy, expiry-
    # limited token (rtm_templates.resolve_download) and 404s anything unknown/expired. Owner-only in
    # practice (the link is only ever surfaced in the owner's own chat/cockpit). GET only: it serves a
    # file, never mutates. Do NOT add mutating RTM routes here (generate stays under /cockpit, nonce-gated).
    "/rtm/doc",
    # The login page + endpoint must be reachable WITHOUT auth (it's how a browser
    # authenticates). The POST only sets a session cookie when the supplied token
    # matches MONGOOSE_API_TOKEN, so this is not a bypass.
    "/login",
    # Username/password login for self-service accounts. Like /login, it must be reachable
    # pre-auth (it's how a phone user with their own account authenticates). It only issues a
    # session cookie when the supplied username+password verify against a stored scrypt hash,
    # so it is not a bypass. Account CREATION (/account/register) is NOT public, it requires an
    # already-authenticated caller and is intentionally absent from this list.
    "/account/login",
    # Web Push subscription plumbing: the public VAPID key is public by design, and subscribe
    # must be reachable by an account/device browser after login/pairing even when the full
    # owner Cockpit remains localhost-only. /push/subscribe has its own in-route auth check;
    # unauthenticated LAN clients cannot register a subscription.
    "/push/vapid-key",
    "/push/subscribe",
    # Symmetric with subscribe (external audit, additional findings #7): a device that can
    # register a push subscription must be able to remove its own. The route now has its own
    # in-route auth + ownership check, mirroring subscribe.
    "/push/unsubscribe",
    # The first-run flow is the front door a fresh install lands on; it must be reachable
    # before any auth is configured. Read-only state; the actions it links to (pairing,
    # wizard saves) remain individually guarded.
    "/welcome",
    "/cockpit/onboarding",
    # The install progress screen + its polling/pair-code endpoints are the very first thing
    # a fresh install shows, before any auth exists. /install/pair-code has its own narrow
    # first-run-only guard inside (ready + nothing paired + tokenless-if-remote).
    "/install",
    # The first-run tutorial runs right after pairing, before auth is set up. It exposes no
    # private data (capability examples + a worked sample), so it's safe pre-auth.
    "/tutorial",
    # PWA plumbing the BROWSER fetches itself (manifest, service worker, share target), these
    # requests cannot carry the X-Mongoose-Device-Token header, so they must be public or install,
    # the offline shell, and the share target all silently fail. Safe: the manifests are static
    # name/icon/theme JSON with no private data, the service worker is generic network-first JS, and
    # /share-target is an inert page whose JS posts to /device/share using the token held on-device.
    "/manifest.webmanifest",
    "/device/manifest.webmanifest",
    "/sw.js",
    "/share-target",
)
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _is_device_route(path: str) -> bool:
    """Routes a paired-device token may authenticate to. Keep deliberately narrow:
    a phone/tablet credential is scoped to its device surface, not the full API."""
    return path == "/device" or path.startswith("/device/")


# Guest devices (trusted_guest) are default-DENY on the device surface: a guest token may reach ONLY
# the explicitly guest-safe routes below. Everything else under /device/* (uploads, the asset store,
# notification ingest, the share bus, owner readiness/router cards, the private tutorial, owner
# summaries, device-event intake) is owner-only. The earlier leak came from an allow-ALL gate
# (any /device/* path), so the fix is to invert it: default-deny means a newly added owner /device/*
# route is closed to guests automatically, instead of silently becoming guest-reachable.
_GUEST_DEVICE_ROUTES = frozenset({
    "/device", "/device/whoami", "/device/capabilities", "/device/profile", "/device/heartbeat",
    "/device/surprise", "/device/delight", "/device/entertain", "/device/summary",
    "/device/tutorial", "/device/tutorial/cool",
    "/device/guest/can-do", "/device/guest/task", "/device/guest/people", "/device/guest/bug",
    "/device/messages", "/device/messages/send", "/device/messages/read",
    "/device/chat", "/device/chat/history", "/device/chat/poll", "/device/self/name", "/device/voice",
    "/device/irc/send", "/device/irc/history",
})

# A trusted guest may reach its OWN account-identity routes remotely, so the credential handoff
# works over the tunnel: create a personal login, sign in, see who it is, sign out. These are
# self-scoped. Account creation still enforces the display-name / username collision guards, and
# none of these grant any owner surface (no setup, home control, memory, or Cockpit writes).
_GUEST_ACCOUNT_ROUTES = frozenset({
    "/account/register", "/account/login", "/account/me", "/account/logout",
    # Poke is the social point of a guest identity: see who is around and poke them (and be
    # poked). These carry the device token AND the account session cookie (device token passes
    # this guard; the cookie identifies who is poking). /poke/<username> is matched by prefix.
    "/users", "/poke/history",
})


def _guest_conv_id(dev: dict) -> str:
    """The conversation id for a guest device, keyed to the PERSON, not the device.

    Invites are single-use, so the same friend coming back on a new device (or after a revoke)
    gets a new device_id; keying the thread on device_id meant their history silently restarted
    from blank and the old thread was orphaned. guest_profiles already records which person a
    device belongs to (owner_label, the owner's own name for them, e.g. "Jo"), so the thread
    key is that person: two invites for "Jo" continue ONE conversation. Falls back to the old
    device-scoped id when no profile row exists (pre-invite pairings), and migrates this
    device's legacy device-scoped thread into the person thread once, idempotently, so nothing
    already said is lost by this change."""
    device_id = str(dev.get("device_id") or "")
    fallback = f"guest:{device_id}"
    try:
        from .db import connect
        with connect() as con:
            row = con.execute("SELECT owner_label FROM guest_profiles WHERE device_id=?",
                              (device_id,)).fetchone()
            label = (row["owner_label"] if row else "") or ""
            label = " ".join(label.split()).casefold()
            if not label:
                # No guest_profiles row (e.g. a pre-invite pairing, or a second device for the same
                # person): fall back to the device's own display name so two devices for one person
                # (e.g. both named "Sam") still share ONE person thread instead of splitting. This was
                # T04: Sam's second device stayed device-scoped, so questions/answers landed in two
                # threads and the interview kept re-asking.
                label = " ".join(str(dev.get("display_name") or "").split()).casefold()
            if not label:
                return fallback
            person_conv = f"guest:p:{label}"
            # The messages table has a foreign key to conversations(id): guarantee the person
            # thread's row exists BEFORE migrating anything into it, or the UPDATE fails for
            # exactly the guests who most need the migration (legacy thread, no person-thread
            # chat yet) and the silent fallback would flip their id back to device-scoped.
            con.execute(
                "INSERT OR IGNORE INTO conversations(id, title, created_at, updated_at) "
                "VALUES (?, ?, datetime('now'), datetime('now'))",
                (person_conv, f"guest {label}"))
            # One-time forward migration of this device's pre-fix thread. Idempotent: after the
            # first pass there are no rows left under the legacy id, and new writes all target
            # the person thread.
            con.execute("UPDATE messages SET conversation_id=? WHERE conversation_id=?",
                        (person_conv, fallback))
            con.commit()
            return person_conv
    except Exception:
        return fallback


def _guest_allowed_device_route(path: str) -> bool:
    """Whether a trusted_guest token may reach this device route (allowlist; default-deny)."""
    return path in _GUEST_DEVICE_ROUTES


def _safe_path_for_log(path: str) -> str:
    """Redact the bearer token out of a guest-invite URL before it reaches a log line, request body,
    proxy log, or anything else that records the path. The invite token in `/guest/invite/<token>`
    is a credential, logging it verbatim is the same class of mistake as logging a password in a URL."""
    try:
        if path.startswith("/guest/invite/"):
            tail = "/claim" if path.endswith("/claim") else ""
            return "/guest/invite/[redacted]" + tail
    except Exception:
        pass
    return path


def _safe_query_for_log(query: str) -> str:
    """Redact a query string for logging: keep parameter names for diagnosis, mask secrets (tokens,
    OAuth codes, passwords, state), cap length so a huge search query can't spam the log."""
    if not query:
        return ""
    try:
        from urllib.parse import parse_qsl, urlencode
        sensitive = {"token", "api_token", "code", "password", "secret", "client_secret", "state"}
        pairs = []
        for k, v in parse_qsl(query, keep_blank_values=True):
            lk = k.lower()
            if any(x in lk for x in sensitive):
                pairs.append((k, "[redacted]"))
            else:
                pairs.append((k, v[:120] + ("…" if len(v) > 120 else "")))
        out = urlencode(pairs)
        return out[:400] + ("…" if len(out) > 400 else "")
    except Exception:
        return "[unparseable-query]"


def _browser_origin_is_trusted(request: Request) -> bool:
    """Reject browser-origin writes from other sites, without breaking curl/CLI/local agents.

    Localhost-only is not a complete browser defence: a hostile web page can still try to submit forms
    or fetches to http://127.0.0.1:8765. CORS stops the attacker reading the response, but not always
    sending the request. Therefore any request that looks browser-originated (Origin or Referer present)
    must come from the same origin as the Mongoose UI. Non-browser clients usually send neither header
    and continue to work as before.
    """
    from urllib.parse import urlparse

    def _hostport(value: str) -> str:
        try:
            u = urlparse(value)
            if not u.scheme or not u.netloc:
                return ""
            host = (u.hostname or "").lower()
            port = u.port
            if port is None:
                port = 443 if u.scheme == "https" else 80
            return f"{host}:{port}"
        except Exception:
            return ""

    host = (request.headers.get("host") or "").split("@")[0].lower()
    if not host:
        return True
    if ":" not in host:
        scheme = request.url.scheme or "http"
        host = f"{host}:{443 if scheme == 'https' else 80}"
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if not value:
            continue
        # Browser-generated Origin may be the literal string "null" for sandboxed/file contexts; it is
        # not a trustworthy same-origin proof, so refuse it for writes.
        if value == "null":
            return False
        hp = _hostport(value)
        if hp and hp != host:
            return False
    return True


@app.middleware("http")
async def _request_observability(request, call_next):
    """Structured request logging, not just uvicorn's "200 OK" lines. Uvicorn only tells us a response
    completed; it can't show which operation began and got stuck, nor explain a long quiet gap. This adds
    a request id, START/DONE lines for non-noisy routes, duration + slow-request warnings, and exception
    traces tagged with the same id. So if Mongoose freezes, the log shows the last request.start that
    never got a request.done, naming exactly what hung. Health polling stays quiet unless it is slow."""
    import logging
    import time
    import uuid
    log = logging.getLogger("mongoose.request")
    path = _safe_path_for_log(request.url.path)
    method = request.method
    rid = uuid.uuid4().hex[:8]
    client = request.client.host if request.client else "?"
    noisy_exact = {"/health", "/health/live", "/install/progress", "/cockpit/nonce"}
    noisy = path in noisy_exact or path.startswith("/static/")
    query = _safe_query_for_log(request.url.query)
    start = time.perf_counter()
    request.state.request_id = rid
    if not noisy:
        log.info("request.start id=%s method=%s path=%s%s client=%s", rid, method, path,
                 ("?" + query) if query else "", client)
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - start) * 1000
        log.exception("request.error id=%s method=%s path=%s duration_ms=%.1f", rid, method, path, duration_ms)
        raise
    duration_ms = (time.perf_counter() - start) * 1000
    try:
        response.headers["X-Mongoose-Request-ID"] = rid
    except Exception:
        pass
    if not noisy:
        log.info("request.done id=%s method=%s path=%s status=%s duration_ms=%.1f", rid, method, path,
                 getattr(response, "status_code", "?"), duration_ms)
    elif duration_ms > 1500:
        log.warning("request.slow id=%s method=%s path=%s status=%s duration_ms=%.1f", rid, method, path,
                    getattr(response, "status_code", "?"), duration_ms)
    if duration_ms > 5000:
        log.warning("request.very_slow id=%s method=%s path=%s status=%s duration_ms=%.1f", rid, method, path,
                    getattr(response, "status_code", "?"), duration_ms)
    return response


def _is_public_facing() -> bool:
    import os as _os
    return _os.environ.get("MONGOOSE_PUBLIC_FACING", "").strip().lower() in ("1", "true", "yes")


@app.middleware("http")
async def _security_headers(request, call_next):
    """Baseline hardening headers on every response. Cheap, and on a public-facing install
    they are the difference between 'fine' and 'why is the cockpit framable from anywhere'.
    Existing per-route headers win; nothing here overwrites a header a route already set."""
    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "no-referrer")
    # HSTS only when the request actually arrived over TLS (directly or via a forwarder);
    # advertising it on plain http would strand LAN clients on an unservable scheme.
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
    if proto == "https":
        h.setdefault("Strict-Transport-Security", "max-age=15552000")
    return response


@app.middleware("http")
async def _guard_sensitive_routes(request, call_next):
    from fastapi.responses import JSONResponse
    path = request.url.path
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and not _browser_origin_is_trusted(request):
        return JSONResponse(
            status_code=403,
            content={"error": "forbidden",
                     "message": "browser-origin write rejected; use the Mongoose UI or send an API token"},
        )
    prefixes = _PUBLIC_PREFIXES
    # Deliberately internet-facing installs close the first-run surfaces: they exist for a
    # fresh machine on a private LAN, and a public install is by definition long past first
    # run. Loopback (and authed remote) can still reach them; anonymous internet cannot.
    if _is_public_facing():
        prefixes = tuple(pfx for pfx in prefixes
                         if pfx not in ("/welcome", "/install", "/tutorial", "/cockpit/onboarding"))
    is_public = any(path == p or path.startswith(p + "/") for p in prefixes)
    if not is_public:
        client_host = request.client.host if request.client else ""
        settings = get_settings()
        # Loopback (the Mac itself) is trusted by default: physical access to the machine is the trust
        # boundary. But that means anyone at the unlocked Mac, or any local process, has the whole
        # cockpit with no login. Setting MONGOOSE_LOCK_LOCAL=true requires a valid session even on
        # loopback, so the cockpit genuinely locks (and times out via the session TTL). The login page
        # and its assets stay public so you can actually log in.
        lock_local = bool(getattr(settings, "lock_local", False))
        loopback_trusted = (client_host in _LOOPBACK_HOSTS) and not lock_local
        if not loopback_trusted:
            token = getattr(settings, "api_token", "") or ""
            allow_remote = getattr(settings, "allow_remote", False)
            # On loopback with lock_local, allow_remote isn't required: a session cookie suffices.
            allow_session = allow_remote or (client_host in _LOOPBACK_HOSTS and lock_local)
            supplied = request.headers.get("x-mongoose-token", "")
            header_ok = bool(allow_remote and token and _secrets.compare_digest(supplied, token))
            # A paired device (phone/tablet) authenticates with its per-device token in
            # X-Mongoose-Device-Token. It resolves to a non-revoked device or fails;
            # revocation takes effect immediately (device_for_token returns None for
            # revoked devices). Like a header-token caller, it proved it holds a secret,
            # so it is treated as a first-class authenticated caller.
            #
            # A device token is its OWN remote-access grant for the narrow /device/* surface and
            # does NOT depend on allow_remote (which governs the much broader master-API-token
            # surface). Two owner consents are already explicit before a device token can be used
            # remotely: the owner paired the device (generating the code at the Mac and claiming it
            # on the phone), and the owner bound the server to the LAN so the phone can reach it at
            # all. Requiring allow_remote on top of that just dead-ends a freshly paired phone for
            # no security gain, the master surface stays gated regardless.
            device_ok = False
            device_supplied = request.headers.get("x-mongoose-device-token", "")
            # Owner-vs-guest scope. The owner's OWN paired phone (profile primary_phone) IS the
            # owner: it gets the full Cockpit surface, exactly as if sitting at the Mac (home
            # control, scheduler, approvals, setup, build, the lot). A trusted_guest device stays
            # scoped to the narrow /device/* surface only (heartbeat, whoami/status, chat, summary,
            # safe event intake, share-to-Mongoose) and can NEVER reach setup, home control, memory
            # mutation, WhatsApp send, the build runner, or broad Cockpit writes.
            if device_supplied:
                from .devices import device_for_token
                dev = device_for_token(device_supplied)
                if dev is not None:
                    if str(dev.get("profile") or "") == "primary_phone":
                        device_ok = True
                    elif (_guest_allowed_device_route(path) or path in _GUEST_ACCOUNT_ROUTES
                          or path.startswith("/poke/")):
                        device_ok = True
            # A valid session cookie (issued via /login after a token check) counts as
            # remote authorisation, so browsers/tablets on the LAN work once logged in.
            # Also the path for loopback-with-lock_local: a session is how you unlock the Mac console.
            cookie_ok = False
            if allow_session:
                from .session_auth import verify_session, SESSION_COOKIE
                cookie_ok = verify_session(request.cookies.get(SESSION_COOKIE))
            if not (cookie_ok or header_ok or device_ok):
                # A guest holding a VALID device token who hits an owner-only route must not be
                # coached to set owner env vars or use the owner API token (external audit B4).
                # Tell them plainly it is the owner's part; keep the admin remediation for
                # genuinely unauthenticated callers.
                if device_supplied and dev is not None:
                    message = ("This part of Mongoose is just for the owner. Head back to your "
                               "launchpad and carry on.")
                else:
                    message = ("This route is restricted to localhost. To allow remote "
                               "access set MONGOOSE_ALLOW_REMOTE=true and MONGOOSE_API_TOKEN, "
                               "and send it as the X-Mongoose-Token header.")
                accept = request.headers.get("accept", "")
                # A HUMAN who clicked through from the public first-run/install page must not hit a raw
                # JSON 403 dead end. The full cockpit is intentionally localhost-only by default, so for
                # a browser GET of /ui or /cockpit give a friendly page with the exact owner-local URL
                # that works on the Mac running Mongoose. API/script callers (no text/html) still get JSON.
                if request.method == "GET" and path in ("/ui", "/cockpit") and "text/html" in accept.lower():
                    port = getattr(settings, "port", None) or 8765
                    scheme = "https" if request.url.scheme == "https" else "http"
                    local_url = f"{scheme}://localhost:{port}/ui"
                    return HTMLResponse(status_code=403, content=(
                        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                        "<title>Open Mongoose on this Mac</title>"
                        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                        "<style>body{margin:0;background:#16140F;color:#ECE6DA;font:16px/1.6 system-ui,sans-serif;"
                        "display:grid;min-height:100vh;place-items:center;padding:24px}"
                        ".card{max-width:560px;background:#1F1C16;border:1px solid #322D24;border-radius:18px;padding:24px}"
                        "h1{margin:0 0 8px;font-size:26px}p{color:#9C9486}"
                        "a{display:inline-block;margin-top:12px;background:#C9A35B;color:#16140F;text-decoration:none;"
                        "font-weight:700;border-radius:12px;padding:11px 16px}code{color:#E9D9B4}</style></head>"
                        "<body><main class=\"card\"><h1>Open Mongoose on this Mac</h1>"
                        "<p>The full cockpit is private and opens only on <code>localhost</code>, unless you "
                        "deliberately enable remote owner access.</p>"
                        "<p>If this is the Mac running Mongoose, use the button below. If this is a phone, pair it "
                        "from the install QR or code shown on the Mac instead (do not use localhost on the phone).</p>"
                        f"<a href=\"{local_url}\">Open local cockpit</a></main></body></html>"))
                return JSONResponse(
                    status_code=403,
                    content={"error": "forbidden", "message": message},
                )
            # CSRF defence for cookie-authenticated browsers: a cookie is auto-attached
            # by the browser, so cookie-only access must not be enough to MUTATE. Any
            # write method authorised purely by cookie must also carry the cockpit
            # nonce (which a cross-origin/injected page cannot read). A header-token or
            # device-token caller (phone/ntfy/script) already proved it holds the secret,
            # so it is exempt. This guards the WHOLE write surface, not just routes.
            if cookie_ok and not (header_ok or device_ok) and request.method in ("POST", "PUT", "PATCH", "DELETE"):
                nonce = request.headers.get("x-cockpit-nonce", "")
                if not (nonce and _secrets.compare_digest(nonce, _COCKPIT_NONCE)):
                    return JSONResponse(
                        status_code=403,
                        content={"error": "forbidden",
                                 "message": "cookie-authenticated writes require the X-Cockpit-Nonce "
                                            "header (CSRF protection). Fetch it from /cockpit/nonce."})
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, str | int]:
    settings = get_settings()
    # "name" is the process-identity token: the installer and stop scripts grep this exact string
    # from /health to prove "the thing on this port is our Mongoose, not some other server." It must
    # stay in lockstep with the greps in the .command launchers and scripts/*.sh. It is not a
    # user-facing brand string. db_path was removed: it revealed the filesystem layout to any
    # unauthenticated LAN client and has no value in a health response.
    return {
        "ok": "true",
        "name": "mongoose",
        "version": __version__,
        "port": settings.port,
    }


@app.post("/device/remote/proof")
async def device_remote_proof(request: Request) -> dict:
    """A paired device records that it just reached this Mac's PUBLIC address successfully,
    from wherever it actually is right now (typically the owner's phone on cellular data,
    genuinely outside the home network). This is the honest fix for a real limitation stated
    plainly elsewhere in this codebase: a self-test the Mac runs against its OWN public
    hostname:port can prove DNS is correct and that the Mac can reach itself, but it can never
    prove a genuinely external client can reach it, only a real external vantage point can.
    The owner's own paired phone, actually on cellular, IS that vantage point; no third-party
    port-checking service is trusted for this.

    FIX (caught by testing my own earlier version against a real guest token): the actual
    risk here was never "a guest calls this route", it is that the route blindly recorded
    WHATEVER url string the caller sent, from ANY caller, with no check that it matched
    anything real. A stray docstring claim that "a guest cannot forge proof" was not backed
    by any code that checked the caller's profile, and even a profile check would have missed
    the deeper problem: an OWNER device could just as easily send a wrong or fabricated URL
    by mistake or by bug. The url the caller reports reaching is now compared against the
    Mac's own current public_url(); only a match is ever recorded as proof."""
    dev = _device_from_request(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    reached_url = str(payload.get("url") or "").strip()[:300]
    try:
        from .ddns import public_url
        from .setup_wizard import effective_env
        configured_url = (public_url(effective_env()) or "").rstrip("/")
    except Exception:
        configured_url = ""
    reached_normalized = reached_url.split("#")[0].split("?")[0]
    for suffix in ("/health", "/device/launchpad", "/remote/ping"):
        if reached_normalized.endswith(suffix):
            reached_normalized = reached_normalized[: -len(suffix)]
    reached_normalized = reached_normalized.rstrip("/")
    if not configured_url or not reached_normalized or reached_normalized != configured_url:
        return {"ok": False, "error": "url_mismatch",
                "detail": "That doesn't match this Mac's currently configured public address, "
                          "so it wasn't recorded as proof."}
    from .db import set_app_state, utc_now_iso
    now = utc_now_iso()
    set_app_state("remote_external_proof_at", now)
    set_app_state("remote_external_proof_url", reached_url)
    set_app_state("remote_external_proof_device", str((dev or {}).get("device_id") or ""))
    return {"ok": True, "recorded_at": now}


@app.get("/pairing/preflight")
def pairing_preflight_route() -> dict:
    """Machine-detected pairing readiness: the server's real scheme + bind state, and candidate URLs
    with honest labels and status, so the user never has to guess which network cause is true. Public
    (it must work before any auth, and it's the recovery path). Never throws: a probe failure shows as a
    degraded field, not a 500."""
    try:
        from .services.pairing_preflight import preflight
        return preflight(verify_local=True)
    except Exception as exc:
        # This is a PUBLIC endpoint (pre-auth recovery path) and preflight() is itself fully defensive,
        # so this outer guard should essentially never fire. If it does, do NOT echo str(exc) to an
        # unauthenticated caller, an unexpected error could carry a filesystem path. Log it server-side
        # and return a generic, honest degraded payload.
        try:
            import logging
            logging.getLogger("mongoose.pairing").warning("preflight failed: %s", exc)
        except Exception:
            pass
        return {"ok": False, "error": "preflight check could not run; see server logs",
                "server": {"scheme": "unknown", "https_enabled": False}, "urls": [], "warnings": []}


@app.get("/health/live")
async def health_live() -> dict:
    """Async liveness probe for the UI watchdog. Because this is async, it only returns promptly if
    the event loop is actually servicing coroutines, if a blocking call has wedged the loop, this
    endpoint stops answering even though the sync /health (served from the threadpool) still does.
    The UI watchdog pings this; when it goes unresponsive while /health stays up, the loop is wedged
    and the UI shows a clear 'the assistant is busy/!stuck' state and offers a restart, rather than
    silently freezing. Also returns the scheduler heartbeat age so a stalled background loop is
    visible. Deliberately does NO model / DB-heavy work."""
    import time as _time
    out: dict = {"ok": True, "loop": "responsive", "ts": _time.time()}
    try:
        from .db import get_app_state
        hb = get_app_state("scheduler_heartbeat")
        if hb:
            try:
                out["scheduler_heartbeat_age_s"] = max(0.0, _time.time() - float(hb))
            except Exception:
                out["scheduler_heartbeat_age_s"] = None
    except Exception:
        pass
    # The in-process runtime watchdog's own status (loop lag, scheduler restarts). None until it has
    # beaten once. Lets the cockpit show that background self-healing is active.
    try:
        from .services.runtime_watchdog import current_status
        ws = current_status()
        if ws is not None:
            out["runtime_watchdog"] = ws
    except Exception:
        pass
    try:
        from .services.resource_guard import current_process_snapshot, system_memory_snapshot
        out["resources"] = {"current_process": current_process_snapshot(),
                            "system_memory": system_memory_snapshot()}
    except Exception:
        pass
    return out


# Short-TTL cache for expensive GLOBAL read-only status endpoints. The cockpit (and a second browser)
# poll these repeatedly; each recompute is 200-800ms, and in aggregate they are what melted the event
# loop. The fetch-concurrency gate paces the initial herd; this makes repeat polls and a second tab
# effectively free. Values are the same JSON-serialisable dicts the endpoints already return, and the
# data is global (not per-user), so one cached value is correct for every caller.


async def _status_cached_async(key: str, ttl: float, fn):
    """Async SWR twin of _status_cached: serve stale immediately, refresh via a background task."""
    now = _status_time.time()
    hit = _STATUS_CACHE.get(key)
    if hit is None:
        val = await fn()
        _STATUS_CACHE[key] = (_status_time.time(), val)
        return val
    ts, val = hit
    if now - ts >= ttl and key not in _STATUS_REFRESHING:
        _STATUS_REFRESHING.add(key)
        import asyncio as _a

        async def _refresh():
            try:
                v = await fn()
                _STATUS_CACHE[key] = (_status_time.time(), v)
            except Exception:
                pass
            finally:
                _STATUS_REFRESHING.discard(key)
        _a.create_task(_refresh())
    return val


@app.get("/doctor")
def doctor() -> dict:
    return _status_cached("doctor", 30.0, run_doctor)


@app.get("/cockpit/wiring")
def cockpit_wiring(request: Request) -> dict:
    """Self-reflection: how Mongoose is wired (routes, intents, jobs, capabilities, chat-surface matrix)
    plus a self-AUDIT of missing wires: cross-surface feature parity and unwired intents. This is the
    'know how I'm wired' surface; a red audit means a feature is wired on one surface but not another
    (the class of bug behind the guest music-onboarding breach). Owner-gated."""
    _require_cockpit_nonce(request)
    from .services.introspection import wiring_map, audit
    return {"ok": True, "map": wiring_map(), "audit": audit()}


@app.get("/cockpit/benchmarks")
def cockpit_benchmarks() -> dict:
    """Benchmark visibility: when benchmarks last ran (they run only in the 5am-8am window), what they
    found, and when Mongoose last surfaced a benchmark revelation. Read-only."""
    from .services.benchmark_surpriser import latest_benchmark_visibility, benchmark_history
    data = latest_benchmark_visibility()
    try:
        data["history"] = benchmark_history()
    except Exception:
        data["history"] = []
    return data


@app.post("/cockpit/benchmarks/enable")
async def cockpit_benchmarks_enable(request: Request) -> dict:
    """Turn the overnight benchmark lane on/off (persists MONGOOSE_BENCHMARK_ENABLED to .env).
    Nonce-gated. Enabling does NOT run a benchmark now: the job stays gated to its quiet
    early-morning window, so the first real run happens overnight, not on this click."""
    _require_cockpit_nonce(request)
    from .setup_wizard import write_env
    payload = await request.json()
    want = bool(payload.get("enabled", True))
    write_env({"MONGOOSE_BENCHMARK_ENABLED": "true" if want else "false"})
    from .services.benchmark_surpriser import latest_benchmark_visibility
    return {"ok": True, "enabled": want, "note": (
        "Benchmarking will run in its overnight window; it does not run now."
        if want else "Overnight benchmarking is off."),
        "visibility": latest_benchmark_visibility()}


@app.get("/cockpit/snapshots")
def cockpit_snapshots(request: Request) -> dict:
    """Brain snapshots: the daily local restore points of the brain (data/brain_snapshots/<stamp>/).
    They are cheap to keep but add up (roughly 0.8 GB each), and until now were only visible/prunable
    from the CLI. Read-only list with per-snapshot size and the running total. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .brain_preservation import list_snapshots
    snaps = list_snapshots(limit=60)
    total = sum(int(s.get("db_bytes") or 0) for s in snaps)
    return {"ok": True, "snapshots": snaps, "count": len(snaps), "total_bytes": total}


@app.post("/cockpit/snapshots/prune")
async def cockpit_snapshots_prune(request: Request) -> dict:
    """Prune brain snapshots to the newest N (default 14, matching the scheduler's own retention), so
    the owner can reclaim disk without the terminal. Nonce-gated. Returns the post-prune list."""
    _require_cockpit_nonce(request)
    from .brain_preservation import list_snapshots, prune_snapshots
    keep = 14
    try:
        payload = await request.json()
        if isinstance(payload, dict) and payload.get("keep") is not None:
            keep = max(1, int(payload.get("keep")))
    except Exception:
        pass
    pruned = prune_snapshots(keep=keep)
    snaps = list_snapshots(limit=60)
    total = sum(int(s.get("db_bytes") or 0) for s in snaps)
    return {"ok": True, "pruned": pruned, "snapshots": snaps, "count": len(snaps),
            "total_bytes": total}


@app.get("/cockpit/backup-status")
def cockpit_backup_status() -> dict:
    """Backup lane visibility: last successful backup, last error (if any), Drive connection, where the
    backups live (folder link), and the grandfather-father-son breakdown, so a silently failing or
    invisible backup becomes visible. Read-only."""
    from .backup_drive import backup_status
    return backup_status()


@app.get("/cockpit/drive-folder")
def cockpit_drive_folder() -> dict:
    """Exactly where Mongoose puts your Drive backups: folder name + a clickable link to open it in
    Drive, plus the drive.file scope note (Mongoose can only see its own folder). Read-only."""
    from .backup_drive import drive_folder_info
    return drive_folder_info()


@app.post("/cockpit/drive-connect")
async def cockpit_drive_connect(request: Request) -> dict:
    """Connect Google Drive for backups (drive.file scope: Mongoose's own folder only). Runs the OAuth
    consent off-thread. Requires the Google OAuth client JSON to be set up first (same one used for
    Gmail/Calendar). Returns ok + where the backups will live."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    from .backup_drive import connect_drive, drive_folder_info
    res = await _asyncio.to_thread(connect_drive)
    if res.get("ok"):
        try:
            res["folder"] = await _asyncio.to_thread(drive_folder_info)
        except Exception:
            pass
    return res


# Phone-capable Drive OAuth: one-time states minted by the nonce-gated start below, validated by the
# public callback. Persisted in the DB (not memory) because the flow spans a browser round-trip and the
# server can restart mid-flow (watchdog/deploy) , an in-memory state would be lost and the callback
# would wrongly report "expired". The state is consumed only after a SUCCESSFUL token exchange, so a
# failed or prefetched callback hit does not burn a still-valid attempt.
def _drive_state_put(state: str, code_verifier: str = "", ttl: float = 600.0) -> None:
    import json as _json
    from .db import get_app_state, set_app_state
    now = _status_time.time()
    try:
        cur = _json.loads(get_app_state("drive_oauth_pending") or "{}")
    except Exception:
        cur = {}
    cur = {k: v for k, v in cur.items()
           if isinstance(v, dict) and v.get("exp", 0) > now}  # prune expired
    cur[state] = {"exp": now + ttl, "cv": code_verifier}
    set_app_state("drive_oauth_pending", _json.dumps(cur))


def _drive_state_get(state: str):
    """Return the code_verifier (possibly '') for a still-valid state, or None if unknown/expired."""
    import json as _json
    from .db import get_app_state
    if not state:
        return None
    try:
        cur = _json.loads(get_app_state("drive_oauth_pending") or "{}")
    except Exception:
        return None
    e = cur.get(state)
    if isinstance(e, dict) and e.get("exp", 0) > _status_time.time():
        return e.get("cv", "")
    return None


def _drive_state_consume(state: str) -> None:
    import json as _json
    from .db import get_app_state, set_app_state
    try:
        cur = _json.loads(get_app_state("drive_oauth_pending") or "{}")
    except Exception:
        cur = {}
    if cur.pop(state, None) is not None:
        set_app_state("drive_oauth_pending", _json.dumps(cur))


def _drive_callback_uri() -> str:
    """The public https callback Google redirects the phone browser back to, via the tunnel. Must match
    EXACTLY what is registered in the Google client's Authorized redirect URIs."""
    from .access_urls import configured_public_base
    base = (configured_public_base() or "").rstrip("/")
    return f"{base}/oauth/drive/callback"


@app.post("/cockpit/drive-connect-url")
async def cockpit_drive_connect_url(request: Request) -> dict:
    """Start the phone-capable Drive connect: return the Google consent URL to open on any device. The
    consent finishes at the public /oauth/drive/callback. Owner-only (nonce-gated); mints a one-time
    state the callback validates."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    from .backup_drive import web_authorization_url
    uri = _drive_callback_uri()
    if not uri.startswith("https://"):
        return {"ok": False, "error": "No public https base is configured (tunnel), so a phone link "
                                      "cannot be built. Use the on-Mac connect instead."}
    try:
        url, state, cv = await _asyncio.to_thread(web_authorization_url, uri)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Could not start Google sign-in: {exc}"}
    _drive_state_put(state, cv, 600.0)              # 10 min window + PKCE verifier, survives a restart
    return {"ok": True, "url": url, "redirect_uri": uri}


def _drive_oauth_result_html(ok: bool, message: str) -> str:
    """Self-contained mobile-friendly result page for the Drive OAuth callback (no external assets)."""
    icon = "✅" if ok else "⚠️"
    head = "Google Drive connected" if ok else "Could not connect"
    color = "#1f9d55" if ok else "#c0392b"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Mongoose backups</title></head>"
        "<body style='margin:0;background:#111;color:#eee;font-family:-apple-system,system-ui,sans-serif;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center'>"
        "<div style='max-width:22rem;padding:1.5rem;text-align:center'>"
        f"<div style='font-size:3rem'>{icon}</div>"
        f"<h1 style='font-size:1.2rem;color:{color};margin:.5rem 0'>{head}</h1>"
        f"<p style='color:#bbb;line-height:1.5'>{message}</p>"
        "<p style='color:#777;font-size:.85rem;margin-top:1.5rem'>You can close this tab and return to "
        "Mongoose.</p></div></body></html>"
    )


@app.get("/oauth/drive/callback")
async def oauth_drive_callback(request: Request):
    """Public Drive OAuth callback (reached by the phone browser via the tunnel). Validates the one-time
    state minted by /cockpit/drive-connect-url, exchanges the code for the dedicated drive.file token,
    and saves it. Refuses any unknown/expired state."""
    from fastapi.responses import HTMLResponse
    import asyncio as _asyncio
    q = request.query_params
    err, state, code = q.get("error", ""), q.get("state", ""), q.get("code", "")
    if err:
        return HTMLResponse(_drive_oauth_result_html(False, f"Google reported: {err}."), status_code=400)
    cv = _drive_state_get(state)
    if cv is None:
        return HTMLResponse(_drive_oauth_result_html(
            False, "This sign-in link has expired or was already used. Open the Backups card in "
                   "Mongoose and start again."), status_code=400)
    if not code:
        return HTMLResponse(_drive_oauth_result_html(False, "Google returned no authorization code."),
                            status_code=400)
    try:
        from .backup_drive import finish_web_consent
        await _asyncio.to_thread(finish_web_consent, _drive_callback_uri(), code, cv)
    except Exception as exc:  # noqa: BLE001
        # Keep the state (still within TTL) so a genuine retry works; only a success consumes it.
        return HTMLResponse(_drive_oauth_result_html(False, f"Could not finish sign-in: {exc}"),
                            status_code=500)
    _drive_state_consume(state)
    return HTMLResponse(_drive_oauth_result_html(
        True, "Backups will now also be kept safely off this Mac, in your own Google Drive (Mongoose "
              "only ever sees its own backup folder)."))


@app.post("/cockpit/backup-now")
async def cockpit_backup_now(request: Request) -> dict:
    """Run a backup right now: snapshot the brain + config locally, then (if Drive is connected) upload
    and apply the grandfather-father-son rotation. Off-thread so the cockpit doesn't block. Read-only
    toward your wider Drive, only Mongoose's backup folder is touched."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    from .backup_drive import backup_to_drive
    return await _asyncio.to_thread(backup_to_drive)


@app.get("/cockpit/harden")
def cockpit_harden_status() -> dict:
    """Self-harden status: open hardening observations and last scan. Read-only; observes only, never
    changes code, builds, or installs."""
    from .services import self_harden as H
    return H.status()


@app.post("/cockpit/harden/scan")
def cockpit_harden_scan(request: Request) -> dict:
    """Run a cheap local hardening scan now. Non-destructive: records observations from scheduler /
    watchdog / audit signals only. No network calls, no external models, no code changes."""
    _require_cockpit_nonce(request)
    from .services import self_harden as H
    return H.scan_runtime()


@app.post("/cockpit/harden/consistency")
def cockpit_harden_consistency(request: Request) -> dict:
    """Check that subsystems making the same claim use the same logic (find claim divergence). Read-only;
    runs built-in claim probes that reuse existing trusted sources. No network, no code changes."""
    _require_cockpit_nonce(request)
    from .services import claim_consistency as CC
    return CC.run_builtin()


@app.post("/cockpit/harden/review-submit-plan")
async def cockpit_harden_review_submit_plan(request: Request) -> dict:
    """Show privacy/cost implications for a reviewer packet before any submission. Does NOT send data,
    this is the explicit consent-preview layer (what would leave the machine, estimated tokens, risk
    terms found in the payload)."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .services import self_harden as H
    return H.review_submission_plan(body.get("packet") or "", reviewer=body.get("reviewer") or "claude")


@app.post("/cockpit/update-stage/list")
def cockpit_update_stage_list(request: Request) -> dict:
    """List staged update candidates (local only; reads stage records)."""
    _require_cockpit_nonce(request)
    from .updater import list_update_stages
    return {"stages": list_update_stages()}


@app.post("/cockpit/update/fetch")
async def cockpit_update_fetch(request: Request) -> dict:
    """Self-update singularity: Mongoose fetches its OWN next update from the configured source
    (MONGOOSE_UPDATE_URL or MONGOOSE_UPDATE_GITHUB_REPO) and stages it. Apply stays human-gated.
    Body may include {url} to override the configured source for this fetch."""
    _require_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    from .updater import fetch_update_candidate
    import asyncio as _aio
    # fetch_update_candidate is blocking (urllib); run in a thread so we don't block the loop.
    result = await _aio.to_thread(fetch_update_candidate, body.get("url"))
    return result


@app.get("/cockpit/update/check")
async def cockpit_update_check(request: Request) -> dict:
    """Check whether a newer version is available from the configured source WITHOUT staging it.
    Read-only: resolves the source and reads the version, nothing is downloaded into the tree."""
    _require_cockpit_nonce(request)
    from .updater import _resolve_update_url, current_version
    url, kind = _resolve_update_url()
    if not url:
        return {"ok": True, "configured": False,
                "note": "no update source set (MONGOOSE_UPDATE_URL or MONGOOSE_UPDATE_GITHUB_REPO)"}
    return {"ok": True, "configured": True, "source_kind": kind,
            "current_version": current_version()}


@app.post("/cockpit/update-stage/check")
async def cockpit_update_stage_check(request: Request) -> dict:
    """Run pre-apply checks on a staged candidate (manifest, compileall, pyflakes). With runtime_smoke,
    also boots the candidate on a temporary localhost port and verifies it serves correct runtime
    identity BEFORE applying, the 'installed vs actually running' claim made safe at update time."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .updater import check_update_stage
    return check_update_stage(body.get("stage_id") or "", runtime_smoke=bool(body.get("runtime_smoke")))


@app.post("/cockpit/update-stage/rollback-drill")
async def cockpit_update_stage_rollback_drill(request: Request) -> dict:
    """Dry-run the rollback path for a staged candidate (verifies backup + update script + checks exist).
    Applies nothing; tells you that you could roll back before you leap."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .updater import rollback_drill_stage
    return rollback_drill_stage(body.get("stage_id") or "")


@app.post("/cockpit/update-stage/approve")
async def cockpit_update_stage_approve(request: Request) -> dict:
    """Mark a checked staged candidate approved for applying. Does not apply it."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .updater import approve_update_stage
    return approve_update_stage(body.get("stage_id") or "")


import secrets as _secrets

# A per-process setup nonce. The setup page reads it from /setup/status (same
# origin) and must echo it back on /setup/save. A malicious cross-origin page
# cannot read the nonce, so it cannot forge a setup write even against localhost.
_SETUP_NONCE = _secrets.token_urlsafe(24)
# A per-process Cockpit action nonce. The cockpit page reads it from /cockpit/nonce
# (same origin) and must echo it on privileged POSTs (install-missing, draft
# approve/cancel). A cross-origin page or injected script that can't read the nonce
# cannot forge these actions, defence in depth alongside output escaping.
from .web_common import _COCKPIT_NONCE, _require_cockpit_nonce, _require_token_or_cockpit_nonce, _status_cached, _read_upload, _safe_download_name  # noqa: F401
from .web_common import _STATUS_CACHE, _STATUS_REFRESHING, _status_time  # noqa: F401


def _owner_surface(request: Request) -> bool:
    """Does this request come from a surface that IS the owner (cockpit nonce, master API
    token, the owner's own paired phone, or a logged-in session)? Used to protect the ONE
    shared conversation: loopback is trusted for reading and for answering, but writing into
    the owner's permanent history is reserved for surfaces that represent the owner. This is
    the lesson of the smoke-test incident made structural: an anonymous local process could
    (and did) write synthetic exchanges into the permanent brain."""
    try:
        supplied = request.headers.get("x-cockpit-nonce", "")
        if supplied and _secrets.compare_digest(supplied, _COCKPIT_NONCE):
            return True
    except Exception:
        pass
    try:
        settings = get_settings()
        token = getattr(settings, "api_token", "") or ""
        supplied = request.headers.get("x-mongoose-token", "")
        if token and supplied and _secrets.compare_digest(supplied, token):
            return True
    except Exception:
        pass
    try:
        device_supplied = request.headers.get("x-mongoose-device-token", "")
        if device_supplied:
            from .devices import device_for_token
            dev = device_for_token(device_supplied)
            if dev is not None and str(dev.get("profile") or "") == "primary_phone":
                return True
    except Exception:
        pass
    try:
        from .session_auth import verify_session, SESSION_COOKIE
        if verify_session(request.cookies.get(SESSION_COOKIE)):
            return True
    except Exception:
        pass
    return False


def _lan_reachable_now() -> bool:
    """Is the server actually reachable from another device on the LAN right now?

    CRITICAL FIX (caught by a second reviewer's regression triage, external to this session):
    this used to answer purely from config (host=0.0.0.0 and allow_remote=true), which is not
    the same fact. A uvicorn process started BEFORE those settings were saved is still
    genuinely bound to loopback only; config can say "LAN enabled" while the socket keeps
    refusing every connection. That mismatch is exactly what let a QR render with no warning
    while the phone got ERR_CONNECTION_REFUSED: the live-check function this needed
    (_lan_socket_reachable) already existed in this same file for a DIFFERENT caller (the
    repair/restart path) and was simply never wired into the path that decides whether to
    show a scannable QR at all."""
    s = get_settings()
    host = getattr(s, "host", "127.0.0.1")
    allow = getattr(s, "allow_remote", False)
    if host not in ("0.0.0.0", "::") or not bool(allow):
        return False
    return _lan_socket_reachable(s)


def _pairing_reachability_note() -> str | None:
    """A user-facing warning when the pairing URL won't actually work yet because the server isn't
    listening on the LAN. None when reachable."""
    if _lan_reachable_now():
        return None
    # Short sentinel: the JS shows an "Enable phone access" button; no terminal command needed.
    return "not_lan_reachable"


def _phone_reachability_diagnosis() -> dict:
    """Structured reason the owner-phone socket is or isn't reachable, shared by every QR
    surface and by the repair path, so the thing being reported and the thing being fixed are
    always the same fact (external triage: 'restriction vs restoration', a diagnostic is
    only useful if it points at a repair the product can actually perform)."""
    out = {"lan_reachable": False, "effective_host": "", "effective_allow_remote": False,
          "env_file_host": "", "env_file_allow_remote": False, "env_path": "", "reason": "unknown"}
    try:
        s_now = get_settings()
        out["effective_host"] = str(getattr(s_now, "host", "") or "")
        out["effective_allow_remote"] = bool(getattr(s_now, "allow_remote", False))
        out["lan_reachable"] = _lan_reachable_now()
        if out["lan_reachable"]:
            out["reason"] = "reachable"
        elif out["effective_host"] not in ("0.0.0.0", "::") or not out["effective_allow_remote"]:
            out["reason"] = "loopback_or_remote_disabled"
        else:
            out["reason"] = "configured_but_socket_dead"
    except Exception:
        pass
    try:
        from .setup_wizard import effective_env, ENV_PATH as _ENVP
        file_env = effective_env()
        out["env_file_host"] = (file_env.get("MONGOOSE_HOST") or "").strip()
        out["env_file_allow_remote"] = str(file_env.get("MONGOOSE_ALLOW_REMOTE") or "").strip().lower() in (
            "1", "true", "yes", "on")
        out["env_path"] = str(_ENVP)
    except Exception:
        pass
    return out


_PHONE_REPAIR_LAST_STARTED = 0.0


def _schedule_detached_phone_restart() -> dict:
    """Restart Mongoose in a detached helper so a LAN bind actually takes effect: uvicorn
    binds its socket at process start, so writing MONGOOSE_HOST=0.0.0.0 to .env alone changes
    nothing for the process already running. Shared by BOTH the explicit 'Enable phone
    access' button and the pairing-flow auto-repair, so they can never drift into two
    different restart mechanisms again (external triage, point 6 of the first submission).
    Debounced: a burst of polls/retries within 30s reuses the in-flight repair instead of
    spawning a second restart helper."""
    global _PHONE_REPAIR_LAST_STARTED
    import time as _time_pr
    now = _time_pr.monotonic()
    if _PHONE_REPAIR_LAST_STARTED and now - _PHONE_REPAIR_LAST_STARTED < 30.0:
        return {"ok": True, "restarting": True, "already_scheduled": True,
                "detail": "Mongoose is already repairing phone access and restarting."}
    _PHONE_REPAIR_LAST_STARTED = now
    try:
        import os as _os_ep
        import subprocess
        import sys
        from .service import _repo_root, _log_dir
        install_dir = str(_repo_root())
        log_file = _log_dir() / "app.log"
        port = str(getattr(get_settings(), "port", None) or _os_ep.environ.get("MONGOOSE_PORT") or "8765")
        helper = (
            "import time,subprocess,sys\n"
            "time.sleep(1.2)\n"
            "try:\n"
            "    subprocess.run([sys.executable,'-m','mongoose.cli','stop'],cwd=%r,timeout=30)\n"
            "except Exception:\n"
            "    pass\n"
            "subprocess.Popen([sys.executable,'-m','mongoose.cli','serve','--port',%r],"
            "cwd=%r,start_new_session=True)\n"
        ) % (install_dir, port, install_dir)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "ab") as log:
            subprocess.Popen([sys.executable, "-c", helper],
                             stdout=log, stderr=log, start_new_session=True, cwd=install_dir)
        return {"ok": True, "restarting": True,
                "detail": "Mongoose is applying phone access and restarting the listener onto the LAN."}
    except Exception as exc:
        return {"ok": False, "restarting": False, "error": str(exc)[:300]}


def _repair_phone_reachability(trigger: str = "pairing") -> dict:
    """Self-heal before giving up on the QR: write the settings a phone needs, then restart
    the stale listener. Browser-first, no terminal instruction. Reuses
    ensure_phone_access_defaults(), the SAME function the existing Enable-phone button
    already calls: this is a new TRIGGER for existing, already-trusted capability, not new
    capability. Both call sites (pre-auth install, owner-nonce cockpit) remain owner-only;
    this function itself does not weaken that boundary. Only fires if the caller explicitly
    asks (auto_repair flag), never as a silent side effect of merely requesting a pairing
    code."""
    before = _phone_reachability_diagnosis()
    if before.get("lan_reachable"):
        return {"ok": True, "needed": False, "reachable": True, "diagnosis": before}
    try:
        from .enable_phone import ensure_phone_access_defaults
        settings_result = ensure_phone_access_defaults()
    except Exception as exc:
        return {"ok": False, "needed": True, "trigger": trigger, "diagnosis": before,
                "error": f"Could not write phone-access settings: {exc}"[:300]}
    restart = _schedule_detached_phone_restart()
    return {"ok": bool(restart.get("ok")), "needed": True, "trigger": trigger,
            "settings": settings_result, "restart": restart,
            "restarting": bool(restart.get("restarting")),
            "detail": restart.get("detail") or restart.get("error") or "Phone access repair started.",
            "diagnosis": before}


def _guest_reachable_url(request: Request) -> str:
    """For trusted_guest QR codes: prefer the DDNS address so guests can connect from home.
    Falls back to LAN IP if DDNS is not configured."""
    try:
        from .ddns import public_url
        from .setup_wizard import effective_env
        base = public_url(effective_env())
        if base:
            return base
    except Exception:
        pass
    host = _phone_reachable_host(request)
    scheme = "https" if request.url.scheme == "https" else "http"
    return f"{scheme}://{host}"


def _lan_socket_reachable(settings, timeout: float = 0.75) -> bool:
    """Is THIS process actually listening on the LAN right now, proven by a real connect -- not
    inferred from config, which can say 0.0.0.0 while the running process, started earlier, is
    still genuinely bound to loopback only."""
    import socket as _socket
    try:
        from .discovery import _primary_ip
        lan_ip = _primary_ip()
        if not lan_ip:
            return False
        port = int(getattr(settings, "port", 8765) or 8765)
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((lan_ip, port)) == 0
    except Exception:
        return False


def _phone_reachable_host(request: Request) -> str:
    """The host:port a PHONE should use to reach this Mac for pairing.

    The Host header is whatever the browser used. When the cockpit is opened on the Mac itself
    that's 127.0.0.1/localhost, which a separate phone can't reach. In that case substitute the
    Mac's real LAN IP (keeping the port) so the pairing URL/QR actually works from another device.
    A non-loopback Host (already a LAN IP or hostname) is kept as-is.
    """
    host = request.headers.get("host", "")
    port = getattr(get_settings(), "port", 8765)
    if not host:
        host = f"127.0.0.1:{port}"
    hostname = host.split(":")[0]
    portpart = host.split(":")[1] if ":" in host else str(port)
    if hostname in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
        try:
            from .discovery import _primary_ip
            lan = _primary_ip()
            if lan:
                return f"{lan}:{portpart}"
        except Exception:
            pass
    return host





def _device_from_request(request: Request) -> dict:
    """Resolve a paired-device token from a /device/* request. Device tokens are
    deliberately route-scoped in the middleware; this helper never falls back to the
    master token or cockpit cookie."""
    tok = request.headers.get("x-mongoose-device-token", "")
    from .devices import device_for_token
    dev = device_for_token(tok)
    if dev is None:
        raise HTTPException(status_code=403, detail="valid device token required")
    return dev



@app.get("/setup/status")
def setup_status() -> dict:
    try:
        from .setup_wizard import integration_status
        integrations = integration_status()
        return {"integrations": integrations, "setup_nonce": _SETUP_NONCE}
    except Exception as exc:
        return {"integrations": [], "setup_nonce": _SETUP_NONCE,
                "_error": f"setup status degraded: {exc}"}


class SetupSave(BaseModel):
    values: dict[str, str]
    setup_nonce: str | None = None


@app.post("/setup/save")
def setup_save(request: SetupSave) -> dict:
    from .setup_wizard import write_env, integration_status, INTEGRATIONS
    # CSRF guard: require the per-process nonce (fetched from /setup/status by the
    # real setup page). Blocks blind cross-origin writes against localhost.
    if not request.setup_nonce or not _secrets.compare_digest(request.setup_nonce, _SETUP_NONCE):
        raise HTTPException(status_code=403, detail="invalid or missing setup_nonce")
    # Only write KNOWN integration keys, never arbitrary ones, and reject values
    # containing newlines/CRs (which could inject extra .env lines via the API).
    allowed = {f.key for integ in INTEGRATIONS for f in integ.fields}
    clean = {
        k: v for k, v in request.values.items()
        if k in allowed and v and "\n" not in v and "\r" not in v
    }
    rejected = [k for k in request.values if k not in allowed]
    if clean:
        write_env(clean)
    try:
        integrations = integration_status()
    except Exception as exc:
        integrations = []
        rejected.append(f"status refresh failed: {exc}")
    return {"saved": list(clean), "rejected": rejected, "integrations": integrations}


@app.post("/setup/authorize/{provider}")
def setup_authorize(provider: str, request: Request) -> dict:
    """Trigger OAuth consent. Note: Google opens a browser on the SERVER (the Mac
    mini) and Microsoft prints a device code to the server console, so this is
    most useful when run on the machine itself. Returns the result message.
    """
    # CSRF guard (QA-audit P1): like /setup/save, require the per-process setup nonce so a
    # cross-origin page open in the user's browser can't POST to 127.0.0.1 and silently kick
    # off a server-side OAuth flow. The real setup page reads the nonce from /setup/status.
    nonce = request.headers.get("x-setup-nonce", "")
    if not nonce or not _secrets.compare_digest(nonce, _SETUP_NONCE):
        raise HTTPException(status_code=403, detail="invalid or missing setup nonce "
                            "(send it in the X-Setup-Nonce header; fetch from /setup/status)")
    from .oauth import authorize_google, authorize_microsoft
    if provider == "google":
        ok, msg = authorize_google()
    elif provider == "microsoft":
        ok, msg = authorize_microsoft()
    else:
        raise HTTPException(status_code=400, detail="provider must be google or microsoft")
    return {"ok": ok, "message": msg}


@app.get("/setup", response_class=HTMLResponse)
def setup_page() -> str:
    from .setup_wizard import INTEGRATIONS
    cards = []
    for integ in INTEGRATIONS:
        fields = "".join(
            f'<label>{f.prompt if hasattr(f,"prompt") else f.key}'
            f'<input name="{f.key}" type="{"password" if f.secret else "text"}" '
            f'placeholder="{f.key}{" (optional)" if f.optional else ""}"></label>'
            for f in integ.fields
        )
        authorize_btn = ""
        oauth_provider = "google" if integ.slug == "google_calendar" else (
            "microsoft" if integ.slug == "microsoft" else "")
        if oauth_provider:
            authorize_btn = (
                f'<button type="button" class="secondary" onclick="authorize(\'{oauth_provider}\', \'{integ.slug}\')">'
                f'Authorise {integ.name.split()[0]} (consent)</button>'
            )
        cards.append(f'''
        <section class="card" id="card-{integ.slug}">
          <h2>{integ.name}</h2>
          <p class="help">{integ.help_text}</p>
          <form onsubmit="return save(event, '{integ.slug}')">
            {fields}
            <button type="submit">Save &amp; verify</button>
            {authorize_btn}
            <span class="status" id="status-{integ.slug}"></span>
          </form>
        </section>''')
    body = "".join(cards)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Mongoose Setup</title>
<style>
  body {{ font: 15px/1.5 -apple-system, system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.6rem; }}
  .card {{ border: 1px solid #ddd; border-radius: 10px; padding: 1rem 1.25rem; margin: 1rem 0; }}
  .card h2 {{ margin: 0 0 .4rem; font-size: 1.1rem; }}
  .help {{ color: #555; font-size: .9rem; margin: .2rem 0 .8rem; }}
  label {{ display: block; margin: .5rem 0; font-size: .85rem; color: #333; }}
  input {{ display: block; width: 100%; padding: .5rem; margin-top: .2rem; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; }}
  button {{ margin-top: .6rem; padding: .5rem 1rem; border: 0; border-radius: 6px; background: #2a6; color: #fff; cursor: pointer; }}
  button.secondary {{ background: #36c; margin-left: .5rem; }}
  .status {{ margin-left: .75rem; font-size: .85rem; }}
  .ok {{ color: #2a6; }} .warn {{ color: #c60; }}
  .note {{ background:#f6f6f6; border-radius:8px; padding:.75rem 1rem; font-size:.85rem; color:#444; }}
</style></head><body>
<h1>Mongoose setup</h1>
<p class="note">Everything you enter is written to your local <code>.env</code> file on this machine. Nothing is sent anywhere except the service being verified. After saving, run <code>mongoose doctor</code> or reload the API for changes to take effect.</p>
{body}
<script>
async function save(e, slug) {{
  e.preventDefault();
  const form = e.target;
  const values = {{}};
  for (const el of form.elements) {{ if (el.name && el.value) values[el.name] = el.value; }}
  const status = document.getElementById('status-' + slug);
  status.textContent = 'Saving...';
  try {{
    const st = await (await fetch('/setup/status')).json();
    const r = await fetch('/setup/save', {{ method: 'POST', headers: {{'content-type':'application/json'}}, body: JSON.stringify({{values, setup_nonce: st.setup_nonce}}) }});
    const data = await r.json();
    const integrations = Array.isArray(data.integrations) ? data.integrations : [];
    const integ = integrations.find(i => i.slug === slug);
    if (integ && integ.verification) {{
      status.textContent = integ.verification.message;
      status.className = 'status ' + (integ.verification.ok ? 'ok' : 'warn');
    }} else {{
      status.textContent = 'Saved.';
      status.className = 'status ok';
    }}
  }} catch (err) {{ status.textContent = 'Error: ' + err; status.className = 'status warn'; }}
  return false;
}}
async function authorize(provider, slug) {{
  const status = document.getElementById('status-' + slug);
  status.textContent = 'Starting consent on the server (check the Mac mini for a browser window or device code)...';
  status.className = 'status';
  try {{
    const st = await (await fetch('/setup/status')).json();
    const r = await fetch('/setup/authorize/' + provider, {{ method: 'POST', headers: {{'X-Setup-Nonce': st.setup_nonce}} }});
    const data = await r.json();
    status.textContent = data.message || data.detail || 'Consent finished.';
    status.className = 'status ' + (data.ok ? 'ok' : 'warn');
  }} catch (err) {{ status.textContent = 'Error: ' + err; status.className = 'status warn'; }}
}}
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def root_front_door(request: Request) -> HTMLResponse:
    """Forgiving public front door. A person who types only the host/IP after install, or comes back
    weeks later having forgotten the exact route, should not get a 404 or raw auth error. The page is
    privacy-safe: it exposes no local state, just clear links to the Mac-local owner UI, guided setup,
    install progress, and the paired-phone surface."""
    from .cockpit import frontdoor_html
    return HTMLResponse(frontdoor_html(), headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0"})


@app.get("/cockpit", response_class=HTMLResponse)
def cockpit_redirect() -> HTMLResponse:
    """Redirect /cockpit to /ui. The canonical Mongoose front door is /ui; /cockpit 404ed, which meant
    any stale bookmark, shared link, or script that opened the old path showed a Not Found page even
    though the server was perfectly healthy. This redirect makes it forgiving."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/ui", status_code=301)


@app.get("/ui", response_class=HTMLResponse)
def cockpit_ui() -> HTMLResponse:
    """Mongoose Cockpit: local control panel (status, setup, activity, approvals,
    search, models, scheduler). A view layer over existing endpoints.

    Served no-cache: the page is dynamic (it polls endpoints) so caching it has no
    benefit, and a cached copy after an update is exactly how "I updated but still
    see the old cockpit" happens. no-cache forces the browser to revalidate."""
    from .cockpit import cockpit_html
    return HTMLResponse(cockpit_html(), headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0"})


def _fonts_dir():
    from pathlib import Path
    return Path(__file__).resolve().parent / "static" / "fonts"


def _img_dir():
    from pathlib import Path
    return Path(__file__).resolve().parent / "static" / "img"


@app.get("/manifest.webmanifest")
def pwa_manifest():
    """Web app manifest, makes the cockpit INSTALLABLE as a phone/tablet app ('Add to Home Screen').
    This is the practical mobile surface: a standalone, full-screen Mongoose with its own icon, short
    name, and a share-target so the OS share sheet can send links/text straight into Mongoose. It is
    not a native app (no background notification listener, that needs a real native build), but it is
    a real, installable home-screen surface that works today on Android and iOS browsers."""
    import json as _json
    from fastapi.responses import Response
    manifest = {
        "name": "Mongoose",
        "short_name": "Mongoose",
        "description": "Your local-first household assistant.",
        "start_url": "/ui",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#1a1a1a",
        "theme_color": "#1a1a1a",
        "icons": [
            {"src": "/static/img/mongoose-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/img/mongoose-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
        # Share target: the OS share sheet can send a link/text/title into Mongoose, which lands at
        # /share and gets captured (e.g. 'remember this', 'add to tasks').
        "share_target": {
            "action": "/share",
            "method": "GET",
            "params": {"title": "title", "text": "text", "url": "url"},
        },
        "shortcuts": [
            {"name": "Chat", "url": "/ui#chat"},
            {"name": "What changed", "url": "/ui#system"},
            {"name": "Approvals", "url": "/ui#approvals"},
        ],
    }
    return Response(content=_json.dumps(manifest), media_type="application/manifest+json")


@app.get("/device/manifest.webmanifest")
def pwa_device_manifest():
    """Web app manifest for the PAIRED PHONE surface. Separate from the cockpit manifest so that a
    phone installing Mongoose from the launchpad gets a home-screen app that opens the launchpad
    (chat + voice + status), not the desktop cockpit. This is what makes it feel like a real phone
    app rather than a shrunk-down admin console."""
    import json as _json
    from fastapi.responses import Response
    manifest = {
        "name": "Mongoose",
        "short_name": "Mongoose",
        "description": "Talk to your household assistant.",
        "start_url": "/device/launchpad",
        "scope": "/device/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#1a1a1a",
        "theme_color": "#1a1a1a",
        "icons": [
            {"src": "/static/img/mongoose-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/img/mongoose-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
        "share_target": {
            "action": "/share-target",
            "method": "GET",
            "params": {"title": "title", "text": "text", "url": "url"},
        },
    }
    return Response(content=_json.dumps(manifest), media_type="application/manifest+json")


@app.get("/sw.js")
def pwa_service_worker():
    """Service worker, the minimum to make Mongoose installable and resilient on a phone: it caches
    the app shell so the cockpit opens instantly and shows a clean offline page when the Mac is asleep,
    rather than the browser's dinosaur. Deliberately CONSERVATIVE: it is network-first for everything
    (so data is always live), and only falls back to cache for the shell when offline, it never serves
    stale connector/status data. Local-first: no external fetches, ever."""
    from fastapi.responses import Response
    sw = """
const SHELL = 'mongoose-shell-__VER__';
// Immutable, content-hashed UI assets (/ui-asset) and /static live in a STABLE cache that survives
// deploys: the hash in the URL is the correctness guarantee, so there is never a reason to re-download
// them just because the version bumped. This is the aggressive precache: the heavy CSS/JS is cached
// before first use and served cache-first thereafter (instant, and works offline).
const ASSETS = 'mongoose-assets-v1';
// Cache only the public device shell. Caching /ui from a phone can store the localhost-only/forbidden
// owner page because service-worker cache fills cannot attach a device token or cockpit session. For
// desktop Cockpit offline, the fallback below shows an honest offline card instead of a stale/gated UI.
const SHELL_URLS = ['/device/launchpad', '/device/cockpit'];
const ASSET_URLS = __PRECACHE__;  // current /ui-asset/* URLs, injected at serve time
self.addEventListener('install', e => {
  self.skipWaiting();
  e.waitUntil(Promise.all([
    caches.open(SHELL).then(c => c.addAll(SHELL_URLS).catch(()=>{})),
    caches.open(ASSETS).then(c => c.addAll(ASSET_URLS.concat([
      '/static/img/mongoose-192.png', '/static/img/mongoose-512.png', '/static/img/mongoose-32.png'
    ])).catch(()=>{})),
  ]));
});
self.addEventListener('activate', e => {
  // A new version's SW (its cache name embeds __VER__, so a deploy makes it byte-different and the
  // browser installs it on the next launch) clears old caches, takes control, then RELOADS any open
  // window to the fresh shell. This is what breaks a phone out of a stale PWA automatically: the
  // page reload is served no-store + network-first, so it always lands on the new UI. One activate
  // per install means no reload loop.
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== SHELL && k !== ASSETS).map(k => caches.delete(k))))
    .then(() => self.clients.claim())
    .then(() => self.clients.matchAll({ type: 'window' }))
    .then(list => { for (const c of list) { try { const p = c.navigate(c.url); if (p && p.catch) p.catch(()=>{}); } catch (_e) {} } }));
});
// Network-first, with a timeout so a half-open socket on flaky wifi doesn't hang the page
// waiting on the SW. Falls back to the CORRECT cached shell for the surface (device launchpad
// for /device/* navigations, cockpit otherwise) when the Mac is unreachable. Never serves stale
// data for non-navigation requests.
function fetchWithTimeout(req, ms){
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('sw-timeout')), ms);
    fetch(req).then(r => { clearTimeout(t); resolve(r); },
                    e => { clearTimeout(t); reject(e); });
  });
}
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (req.mode === 'navigate') {
    // ONLY the installed device PWA has a cached shell to fall back to. For every other
    // navigation (/, /install, /setup, /cockpit) the SW must not touch the request AT ALL:
    // a SW-mediated fetch can never surface the browser's certificate interstitial, so a
    // changed/renewed local certificate turned into a permanently stuck error page (the
    // interstitial that lets the user re-trust the cert never got the chance to appear,
    // and the fixed sw.js could not even download over the now-untrusted origin). A bare
    // return hands the navigation to the browser natively: interstitials, errors and
    // recovery all work, and a healed origin then picks up the new SW automatically.
    const isDevice = url.pathname.startsWith('/device/');
    if (!isDevice) {
      return;
    }
    // A 5xx from the edge (Cloudflare 502/503/504 while the Mac restarts or is briefly
    // unreachable) is a RESOLVED response, so without this check it passes the raw gateway
    // error page straight through, which is exactly what a tapped notification once showed.
    // Treat 5xx like a hard network failure and serve a friendly self-healing screen that
    // reloads itself, so a brief blip (a restart) recovers on its own and the user never sees
    // Cloudflare's error.
    const degraded = () => new Response(
      '<!DOCTYPE html><meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<style>body{font-family:system-ui;background:#1a1a1a;color:#eee;display:grid;place-items:center;height:100vh;margin:0;text-align:center;padding:20px}h1{font-weight:600}</style>' +
      '<div><h1>Reconnecting to Mongoose\\u2026</h1><p>Mongoose is a moment away (the Mac may be restarting). This screen reconnects on its own.</p>' +
      '<p style="color:#999;font-size:13px">Still here after a minute? The Mac may be asleep, or its local certificate changed: open the site in a fresh tab and accept the certificate prompt.</p></div>' +
      '<script>setTimeout(function(){location.reload()},5000)</script>' +
      '<noscript><meta http-equiv="refresh" content="6"></noscript>',
      {headers: {'Content-Type': 'text/html'}});
    // STALE-WHILE-REVALIDATE for the phone shells: serve the cached shell INSTANTLY (fast open, offline
    // safe), refresh it in the background, and if the fetched shell DIFFERS from the cached one (a deploy
    // shipped new content-hashed asset bundles) reload the open windows ONCE so the new UI lands on its
    // own, with no wait on the network at open time and no dependence on iOS updating the worker. This
    // replaces both the old cache-first (never updated -> "cleared cache, no change") and the brief
    // network-first (fetched on every open -> "takes ages to load"). Only if nothing is cached do we wait
    // on the network, then fall back to the cockpit shell, then the self-healing reconnect screen.
    e.respondWith((async () => {
      const cached = await caches.match(req);
      const netP = fetchWithTimeout(req, 6000).then(async r => {
        if (r && r.ok) {
          const c = await caches.open(SHELL);
          const prev = await c.match(req);
          await c.put(req, r.clone());
          if (prev) {
            try {
              const [a, b] = await Promise.all([prev.clone().text(), r.clone().text()]);
              if (a !== b) {
                const wins = await self.clients.matchAll({ type: 'window' });
                for (const w of wins) { try { const p = w.navigate(w.url); if (p && p.catch) p.catch(()=>{}); } catch (_e) {} }
              }
            } catch (_e) {}
          }
        }
        return r;
      }).catch(() => null);
      if (cached) return cached;
      const r = await netP;
      if (r && !(r.status >= 502 && r.status <= 504)) return r;
      return (await caches.match('/device/cockpit')) || (await caches.match('/device/launchpad')) || degraded();
    })());
    return;
  }
  // Non-navigation GETs: network-first with timeout, fall back to cache only if present.
  // The install flow is exempt entirely (progress polling included): during an install or
  // handover the server may be mid-restart, and even a well-behaved SW layer adds timeout
  // and cache semantics that the /install page's own polling already handles better natively.
  if (url.pathname === '/install' || url.pathname.startsWith('/install/')) { return; }
  // Immutable assets: cache-first, forever. Content-hashed /ui-asset and versioned /static never change
  // under a given URL, so once cached they are served instantly from the SW without touching the
  // network, and they work offline. A changed asset ships under a new hash/URL, so this is never stale.
  if (url.pathname.startsWith('/ui-asset/') || url.pathname.startsWith('/static/')) {
    e.respondWith(caches.open(ASSETS).then(c => c.match(req).then(hit =>
      hit || fetchWithTimeout(req, 10000).then(r => { if (r && r.ok) c.put(req, r.clone()); return r; })
                .catch(() => hit))));
    return;
  }
  e.respondWith(fetchWithTimeout(req, 10000).catch(() => caches.match(req)));
});
// Web Push: show the notification the server sent (works even when the app is closed). The payload
// is JSON: {title, body, url, tag, icon}.
self.addEventListener('push', e => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch (err) { data = { body: (e.data && e.data.text()) || '' }; }
  const title = data.title || 'Mongoose';
  const options = {
    body: data.body || '',
    icon: data.icon || '/static/img/mongoose-192.png',
    badge: '/static/img/mongoose-192.png',
    tag: data.tag || undefined,
    data: { url: data.url || '/device/launchpad' },
  };
  e.waitUntil(self.registration.showNotification(title, options));
});
// Tapping a notification focuses an existing window or opens the launchpad.
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || '/device/launchpad';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if ('focus' in c) { c.navigate(target).catch(()=>{}); return c.focus(); }
      }
      if (self.clients.openWindow) return self.clients.openWindow(target);
    })
  );
});
"""
    import json as _json
    from .cockpit import ui_asset_urls
    body = sw.replace("__VER__", __version__).replace("__PRECACHE__", _json.dumps(ui_asset_urls()))
    return Response(content=body, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


_SHARE_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Share to Mongoose</title>
<link rel="icon" href="/static/img/mongoose-192.png">
<style>body{font-family:-apple-system,system-ui,sans-serif;background:#1a1a1a;color:#eee;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;padding:24px}.card{max-width:420px;text-align:center}img{width:72px;height:72px}a.btn{display:inline-block;margin-top:14px;background:#c9a35b;color:#1a1407;text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:700}p{color:#aaa}</style>
</head><body><div class="card"><img src="/static/img/mongoose-192.png" alt=""><h1 id="h">Sharing to Mongoose&hellip;</h1><p id="m"></p><a class="btn" href="/device/launchpad">Open Mongoose</a></div>
<script>
const SHARED=__SHARED__;
const TOKEN=localStorage.getItem('mongoose_device_token');
const h=document.getElementById('h'), m=document.getElementById('m');
if(!TOKEN){ h.textContent='Open Mongoose first'; m.textContent='Pair this device in Mongoose, then sharing will work here.'; }
else {
  const body=[SHARED.title,SHARED.text,SHARED.url].filter(Boolean).join(' ');
  fetch('/device/share',{method:'POST',headers:{'Content-Type':'application/json','X-Mongoose-Device-Token':TOKEN},body:JSON.stringify({text:body})})
    .then(r=>r.json().catch(()=>({})).then(d=>{ if(r.ok&&d&&d.ok!==false){ h.textContent='Shared to Mongoose'; m.textContent='You can close this tab.'; } else { h.textContent='Could not share'; m.textContent=(d&&(d.detail||d.message))||'This device is not allowed to share.'; } }))
    .catch(()=>{ h.textContent='Could not reach Mongoose'; m.textContent='Check your connection and try again.'; });
}
</script></body></html>"""


@app.get("/share-target", response_class=HTMLResponse)
def pwa_share_target_device(title: str = "", text: str = "", url: str = ""):
    """Device PWA share target. An inert page whose JS posts the shared content to /device/share using
    the device token held on THIS device (localStorage). No nonce and no owner data are embedded, so it
    is safe to serve pre-auth, unlike /share, which is the cockpit target and carries the cockpit
    nonce. Guests can't post to /device/share (owner-only), so for them this reports that cleanly."""
    import json as _json
    html = _SHARE_HTML.replace("__SHARED__", _json.dumps({"title": title, "text": text, "url": url}))
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/share")
async def pwa_share_target(title: str = "", text: str = "", url: str = "", request: Request = None):
    """Receive content shared INTO Mongoose from the phone's share sheet. The manifest share_target
    uses GET, but a GET request that silently writes to memory is a CSRF hazard: accidental shares,
    forwarded links, and browser prefetches all become durable writes. Fix: the GET shows a
    confirmation page ("Save this to Mongoose?") and only a POST with the cockpit nonce actually
    saves. Shared items go into a confirmation inbox, not directly to permanent memory."""
    from fastapi.responses import HTMLResponse
    shared = " ".join(p for p in [title, text, url] if p).strip()
    if not shared:
        return HTMLResponse("<!doctype html><html><body style='font-family:system-ui;padding:24px'>"
                            "<p>Nothing to save.</p><a href='/ui'>Open Mongoose</a></body></html>")
    # Show a confirmation page: the user taps "Save" to POST with a nonce, or "Dismiss".
    nonce = _COCKPIT_NONCE
    escaped = shared.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Save to Mongoose?</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;background:#1a1a1a;color:#eee;
display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:24px;text-align:center}}
.card{{background:#252520;border-radius:14px;padding:20px;max-width:400px;width:100%}}
.preview{{background:#1a1a16;border-radius:8px;padding:12px;margin:12px 0;font-size:14px;color:#c9b97a;word-break:break-word;text-align:left}}
button{{width:100%;padding:12px;border-radius:10px;border:none;font-size:15px;margin-top:8px;cursor:pointer}}
.save{{background:#c9a227;color:#1a1a1a}}.dismiss{{background:#333;color:#aaa}}
a{{color:#d4af37;margin-top:16px;display:block}}</style></head>
<body><div class="card"><div style="font-size:17px;font-weight:600">Save to Mongoose?</div>
<div class="preview">{escaped}</div>
<button class="save" onclick="doSave()">Save to notes</button>
<button class="dismiss" onclick="location.href='/ui'">Dismiss</button>
<a href="/ui">Open Mongoose</a></div>
<script>
async function doSave(){{
  const r=await fetch("/share/confirm",{{method:"POST",
    headers:{{"Content-Type":"application/json","X-Cockpit-Nonce":"{nonce}"}},
    body:JSON.stringify({{text:{repr(shared)},cockpit_nonce:"{nonce}"}})
  }});
  const d=await r.json();
  document.body.innerHTML='<div style="padding:24px;text-align:center">'+(d.ok?"Saved.":"Could not save.")+'<br><a href="/ui" style="color:#d4af37">Open Mongoose</a></div>';
}}
</script></body></html>""")


class ShareConfirmRequest(BaseModel):
    text: str
    cockpit_nonce: str = ""


@app.post("/share/confirm")
def pwa_share_confirm(req: ShareConfirmRequest, request: Request) -> dict:
    """Actually save a shared item, after the user confirmed on the /share page. Nonce-gated."""
    _require_cockpit_nonce(request)
    text = (req.text or "").strip()[:500]
    if not text:
        return {"ok": False, "error": "nothing to save"}
    try:
        from .services import memory as mem
        mem.remember_fact(f"Shared to Mongoose: {text}", source="share")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/static/img/{name}")
def static_img(name: str):
    """Serve a bundled logo image (the pixel-art mongoose, local-first, no CDN). Same traversal-safe
    guard as the font route: only files that actually exist in the img dir, matched by exact base
    name, are served."""
    from fastapi.responses import Response
    d = _img_dir()
    allowed = {f.name: f for f in d.glob("*.png")} if d.exists() else {}
    path = allowed.get(name)
    if path is None:
        raise HTTPException(status_code=404, detail="image not found")
    data = path.read_bytes()
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/static/fonts/rajdhani.css")
def static_rajdhani_css():
    """Serve Rajdhani CSS from the local font cache, with a fast local() fallback.

    The guest invite page wants Rajdhani, but we do not block first paint on an
    external font CDN. Owner-side invite creation kicks off a one-time background
    provisioner; this route serves the cached, locally rewritten CSS once present.
    """
    from fastapi.responses import Response
    from .font_assets import rajdhani_css
    css, info = rajdhani_css(kick=True)
    cache = "public, max-age=31536000, immutable" if info.get("ok") else "no-cache"
    return Response(content=css, media_type="text/css; charset=utf-8",
                    headers={"Cache-Control": cache})


@app.get("/static/fonts/provisioned/rajdhani/{name}")
def static_provisioned_rajdhani_font(name: str):
    """Serve Rajdhani woff2 files downloaded into the per-user Mongoose data dir.

    This keeps the invite page local after first provision while avoiding vendored
    font binaries in small patch files. Only exact cached .woff2 basenames are
    served; traversal attempts 404.
    """
    from fastapi.responses import Response
    from .font_assets import resolve_rajdhani_file
    path = resolve_rajdhani_file(name)
    if path is None:
        raise HTTPException(status_code=404, detail="provisioned font not found")
    return Response(content=path.read_bytes(), media_type="font/woff2",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/static/fonts/{name}")
def static_font(name: str):
    """Serve a bundled woff2 font (local-first: no third-party font CDN). Traversal-
    safe: only files that actually exist in the fonts dir, matched by exact base name,
    are served, an unknown or `../`-style name 404s, same guard as the log viewer."""
    from fastapi.responses import Response
    d = _fonts_dir()
    # enumerate, then match by base name only (never join an arbitrary path)
    allowed = {f.name: f for f in d.glob("*.woff2")} if d.exists() else {}
    path = allowed.get(name)
    if path is None:
        raise HTTPException(status_code=404, detail="font not found")
    data = path.read_bytes()
    return Response(content=data, media_type="font/woff2",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/login", response_class=HTMLResponse)
def login_page() -> str:
    """A tiny login page so phones/tablets on the LAN can authenticate once (enter the
    API token) and receive a session cookie. Reachable without auth by design."""
    from .cockpit import login_html
    return login_html()


class LoginRequest(BaseModel):
    token: str
    remember: bool = False


# Simple in-memory per-IP login throttle: after several failures, a short cooldown.
# Token-gated and LAN-scoped, so this is belt-and-braces against brute force, not a
# primary control. Resets on restart (fine for this threat model).
_LOGIN_FAILS: dict = {}
_LOGIN_MAX_FAILS = 5
_LOGIN_COOLDOWN_S = 300


def _login_blocked(ip: str) -> float:
    import time as _t
    rec = _LOGIN_FAILS.get(ip)
    if not rec:
        return 0.0
    fails, until = rec
    if fails >= _LOGIN_MAX_FAILS and until > _t.time():
        return until - _t.time()
    return 0.0


def _login_fail(ip: str) -> None:
    import time as _t
    fails = (_LOGIN_FAILS.get(ip, (0, 0))[0]) + 1
    _LOGIN_FAILS[ip] = (fails, _t.time() + _LOGIN_COOLDOWN_S)


@app.post("/login")
def login(request: LoginRequest, http_request: Request) -> JSONResponse:
    """Exchange the API token for a session cookie. Only sets the cookie when the
    token matches; otherwise 401. Per-IP throttled against brute force. The cookie is
    http-only + SameSite=Lax; mutating actions still need the cockpit nonce."""
    from .session_auth import token_matches, issue_session, cookie_max_age, SESSION_COOKIE
    import logging
    ip = http_request.client.host if http_request.client else "?"
    wait = _login_blocked(ip)
    if wait > 0:
        return JSONResponse(status_code=429,
                            content={"ok": False, "error": f"too many attempts; wait {int(wait)}s"})
    if not token_matches(request.token):
        _login_fail(ip)
        # log the attempt WITHOUT the supplied token
        logging.getLogger("mongoose.auth").warning("failed login from %s", ip)
        return JSONResponse(status_code=401, content={"ok": False, "error": "invalid token"})
    _LOGIN_FAILS.pop(ip, None)  # success clears the counter
    value = issue_session(remember=request.remember)
    resp = JSONResponse(content={"ok": True, "remember": request.remember})
    resp.set_cookie(SESSION_COOKIE, value, max_age=cookie_max_age(request.remember),
                    httponly=True, samesite="lax", path="/")
    return resp


@app.post("/logout")
def logout() -> JSONResponse:
    from .session_auth import SESSION_COOKIE
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ── Self-service user accounts (optional username + password, persistent cross-device sessions) ──────
# Anyone who can already reach the cockpit (already has a session, or is on the trusted Mac, or knows
# the token) can create their OWN username + password. Logging in with those credentials issues a normal
# session cookie, so the same person can log in from a phone, a tablet and a laptop, each holding its own
# persistent session ("remember me" = 30 days per device). Passwords are scrypt-hashed; nothing is stored
# in the clear. See services/auth_accounts.py.

class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str | None = None
    allow_duplicate_display_name: bool = False


class AccountLoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = False


class ChangePasswordRequest(BaseModel):
    username: str
    old_password: str
    new_password: str


def _caller_is_authenticated(request: Request) -> bool:
    """True if the caller is already trusted: on loopback (the Mac, unless lock_local), or holds a valid
    API token header, or a valid session cookie. Used to gate ACCOUNT CREATION so a random unauthenticated
    LAN client cannot mint accounts, only someone who already has access can create a login."""
    client_host = request.client.host if request.client else ""
    s = get_settings()
    lock_local = bool(getattr(s, "lock_local", False))
    if client_host in _LOOPBACK_HOSTS and not lock_local:
        return True
    token = getattr(s, "api_token", "") or ""
    if token:
        header_tok = (request.headers.get("x-api-token", "")
                      or request.headers.get("x-mongoose-token", "")
                      or request.headers.get("authorization", "")).replace("Bearer ", "").strip()
        if header_tok and _secrets.compare_digest(header_tok, token):
            return True
    # A paired primary_phone (owner) device may create its own account identity for Poke, so the
    # owner is not blocked from Poke on the LAN, where the request is not loopback.
    try:
        device_token = request.headers.get("x-mongoose-device-token", "")
        if device_token:
            from .devices import device_for_token
            dev = device_for_token(device_token)
            if dev is not None and str(dev.get("profile") or "") == "primary_phone":
                return True
    except Exception:
        pass
    from .session_auth import verify_session, SESSION_COOKIE
    if verify_session(request.cookies.get(SESSION_COOKIE)):
        return True
    # Any paired device is an authenticated caller: it holds a scoped token proving it was
    # invited. This includes a trusted_guest. Guests were once blocked from minting accounts,
    # but the identity model now gives every paired person a savable login (the guest claim
    # hands them suggested credentials to save, so they can be poked and recognised by name).
    # The abuse this originally guarded against (impersonation, name clashes) is now handled at
    # register itself by the display-name / username collision checks, so the device-token gate
    # no longer needs to exclude guests.
    try:
        from .devices import device_for_token as device_from_token
        dtok = request.headers.get("x-mongoose-device-token", "").strip()
        if dtok:
            d = device_from_token(dtok)
            if d:
                return True
    except Exception:
        pass
    # tokenless first-run (no token configured yet): loopback already returned True above; for a
    # tokenless remote we still refuse account creation.
    return False


@app.post("/account/register")
def account_register(req: RegisterRequest, request: Request) -> JSONResponse:
    """Create a self-service account. Requires an already-authenticated caller (so a paired phone or the
    Mac can create logins, but a random LAN client cannot). On success the new account can immediately be
    used at /account/login from any device. Does NOT auto-issue a session (the client logs in next)."""
    if not _caller_is_authenticated(request):
        return JSONResponse(status_code=401,
                            content={"ok": False, "error": "sign in first to create an account"})
    from .services import auth_accounts as AA
    # Catch a DISPLAY-NAME collision at the door, distinct from the username unique constraint: two
    # people shown under the same name is the confusing accident. Soft check, overridable when the
    # caller confirms it is genuinely a separate person who happens to share a name.
    if req.display_name and not req.allow_duplicate_display_name and AA.display_name_taken(req.display_name):
        return JSONResponse(status_code=409, content={
            "ok": False, "error": "display_name_taken", "display_name": req.display_name})
    res = AA.create_account(req.username, req.password, display_name=req.display_name)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return JSONResponse(content={"ok": True, "username": res["username"],
                                 "display_name": res.get("display_name")})


@app.post("/account/login")
def account_login(req: AccountLoginRequest, http_request: Request) -> JSONResponse:
    """Log in with a username + password and receive a session cookie that works on THIS device. Logging
    in from multiple devices is supported, each gets its own cookie. 'remember' makes it last 30 days.
    Per-IP throttled (shares the /login throttle) against brute force. Generic error on failure."""
    from .session_auth import issue_session, cookie_max_age, SESSION_COOKIE
    from .services import auth_accounts as AA
    import logging
    ip = http_request.client.host if http_request.client else "?"
    wait = _login_blocked(ip)
    if wait > 0:
        return JSONResponse(status_code=429,
                            content={"ok": False, "error": f"too many attempts; wait {int(wait)}s"})
    chk = AA.verify_credentials(req.username, req.password)
    if not chk.get("ok"):
        _login_fail(ip)
        logging.getLogger("mongoose.auth").warning("failed account login from %s", ip)
        return JSONResponse(status_code=401, content={"ok": False, "error": "invalid username or password"})
    _LOGIN_FAILS.pop(ip, None)
    # Bind this device's Web Push subscription to the account, so account-targeted notifications
    # (pokes) reach the phone. A subscription created before login has a NULL user_id and would
    # otherwise never receive a poke (the exact "poked but no notification" symptom).
    try:
        dtok = http_request.headers.get("x-mongoose-device-token", "").strip()
        if dtok:
            from .devices import device_for_token
            dev = device_for_token(dtok)
            if dev and dev.get("device_id"):
                from .services.web_push import bind_device_subscriptions_to_user
                bind_device_subscriptions_to_user(dev["device_id"], chk["user_id"])
                AA.set_account_device(chk["user_id"], dev["device_id"])
    except Exception:
        pass
    value = issue_session(remember=req.remember, user_id=chk["user_id"])
    resp = JSONResponse(content={"ok": True, "username": chk["username"], "remember": req.remember})
    resp.set_cookie(SESSION_COOKIE, value, max_age=cookie_max_age(req.remember),
                    httponly=True, samesite="lax", path="/")
    return resp


@app.post("/device/login")
async def device_login(request: Request) -> JSONResponse:
    """Get back into the app with a username + password after the device token was cleared/lost.
    A correct login mints a FRESH device token for THIS device, with the profile decided by the
    account (owner accounts restore owner access, everyone else gets guest access, the client never
    picks). Public + per-IP rate limited (shares the /login throttle) since a locked-out device has
    no token to present. This is the deliberate 'password is a key to your surface over the tunnel'
    path the owner opted into."""
    from .services import auth_accounts as AA
    ip = request.client.host if request.client else "?"
    wait = _login_blocked(ip)
    if wait > 0:
        return JSONResponse(status_code=429, content={"ok": False, "error": f"too many attempts; wait {int(wait)}s"})
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = str((body or {}).get("username") or "").strip()
    password = str((body or {}).get("password") or "")
    dtype = str((body or {}).get("device_type") or "phone")[:32]
    chk = AA.verify_credentials(username, password)
    if not chk.get("ok"):
        _login_fail(ip)
        return JSONResponse(status_code=401, content={"ok": False, "error": "invalid username or password"})
    _LOGIN_FAILS.pop(ip, None)
    acct = AA.get_account(chk["user_id"]) or {}
    profile = "primary_phone" if AA.is_owner_account(chk["user_id"]) else "trusted_guest"
    display_name = acct.get("display_name") or chk["username"]
    from .devices import register_login_device
    dev = register_login_device(display_name=display_name, profile=profile, device_type=dtype)
    if not dev.get("ok"):
        return JSONResponse(status_code=500, content={"ok": False, "error": "could not restore access"})
    # Point this account's poke identity at the device the person is on NOW, so it stays idempotent
    # and pokes to them reach this phone (their old device is orphaned after clearing the token).
    try:
        AA.set_account_device(chk["user_id"], dev["device_id"])
        AA.rebind_owner_device(chk["user_id"], dev["device_id"])
        from .services.web_push import bind_device_subscriptions_to_user
        bind_device_subscriptions_to_user(dev["device_id"], chk["user_id"])
    except Exception:
        pass
    return JSONResponse(content={"ok": True, "device_token": dev["device_token"],
                                 "profile": dev["profile"], "display_name": dev["display_name"]})


@app.post("/account/set-password")
async def account_set_password(request: Request) -> JSONResponse:
    """Let the OWNER choose a sign-in password for their identity, proven by their paired device (no
    old password needed, because their identity was auto-created with a random one they never saw).
    This is what makes /device/login usable for the owner: set a password here, sign in with it if
    the device token is ever lost. Owner device only; sets the password for the owner account."""
    from .services import auth_accounts as AA
    dtok = request.headers.get("x-mongoose-device-token", "").strip()
    dev = None
    if dtok:
        from .devices import device_for_token
        dev = device_for_token(dtok)
    if not dev or dev.get("profile") != "primary_phone":
        return JSONResponse(status_code=403, content={"ok": False, "error": "owner device required"})
    try:
        body = await request.json()
    except Exception:
        body = {}
    new_password = str((body or {}).get("password") or "")
    # Ensure the owner identity exists + is bound to this device, then set its password.
    dn = (dev.get("display_name") or dev.get("name") or "owner").strip() or "owner"
    acct = AA.get_or_create_owner_account(dev["device_id"], dn)
    if not acct.get("ok"):
        return JSONResponse(status_code=500, content={"ok": False, "error": "could not resolve owner identity"})
    res = AA.set_password(acct["user_id"], new_password)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content={"ok": False, "error": res.get("error", "could not set password")})
    return JSONResponse(content={"ok": True, "username": acct["username"]})


@app.post("/account/owner-identity")
def account_owner_identity(request: Request) -> JSONResponse:
    """Give the OWNER a poke identity with no sign-in wall. The owner's paired device is the
    authority, so this mints (or re-fetches) one poke account bound to that device and logs it in
    by setting a session cookie. Idempotent: safe to call on every load. Guests are refused (they
    got their identity at the invite handoff). Fixes the 'owner has no poke dropdown' trap where a
    client-side password dance could silently fail and leave the owner with no identity at all."""
    from .session_auth import issue_session, cookie_max_age, SESSION_COOKIE
    from .services import auth_accounts as AA
    dtok = request.headers.get("x-mongoose-device-token", "").strip()
    dev = None
    if dtok:
        from .devices import device_for_token
        dev = device_for_token(dtok)
    if not dev or dev.get("profile") == "trusted_guest":
        return JSONResponse(status_code=403, content={"ok": False, "error": "owner device required"})
    dn = (dev.get("display_name") or dev.get("name") or "").strip() or "owner"
    acct = AA.get_or_create_owner_account(dev["device_id"], dn)
    if not acct.get("ok"):
        return JSONResponse(status_code=500, content={"ok": False, "error": acct.get("error", "could not create identity")})
    # Bind this device's push subscription so pokes to the owner actually reach the phone.
    try:
        from .services.web_push import bind_device_subscriptions_to_user
        bind_device_subscriptions_to_user(dev["device_id"], acct["user_id"])
    except Exception:
        pass
    value = issue_session(remember=True, user_id=acct["user_id"])
    resp = JSONResponse(content={"ok": True, "username": acct["username"],
                                 "display_name": acct["display_name"]})
    resp.set_cookie(SESSION_COOKIE, value, max_age=cookie_max_age(True),
                    httponly=True, samesite="lax", path="/")
    return resp


@app.get("/account/me")
def account_me(request: Request) -> dict:
    """Who is logged in on this device, if anyone. Returns the account for a username session, or a
    bearer/token session marker, or anonymous. Read-only, no secrets."""
    from .session_auth import session_user, SESSION_COOKIE
    from .services import auth_accounts as AA
    uid = session_user(request.cookies.get(SESSION_COOKIE))
    if uid is False:
        return {"ok": True, "authenticated": False}
    if uid:
        acct = AA.get_account(uid)
        if acct:
            return {"ok": True, "authenticated": True, "account": True, "username": acct["username"]}
        # a user session whose account was deleted: treat as a plain authenticated session
        return {"ok": True, "authenticated": True, "account": False}
    return {"ok": True, "authenticated": True, "account": False, "via": "token_or_local"}


@app.post("/account/logout")
def account_logout(request: Request) -> JSONResponse:
    """Sign out on this device by clearing the session cookie. Safe to call when not signed in."""
    from .session_auth import SESSION_COOKIE
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.post("/account/change-password")
def account_change_password(req: ChangePasswordRequest, request: Request) -> JSONResponse:
    """Change a password after verifying the current one. Requires an authenticated caller."""
    if not _caller_is_authenticated(request):
        return JSONResponse(status_code=401, content={"ok": False, "error": "sign in first"})
    from .services import auth_accounts as AA
    res = AA.change_password(req.username, req.old_password, req.new_password)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return JSONResponse(content={"ok": True, "username": res["username"]})


# ── User discovery + pokes ────────────────────────────────────────────────────────────────────────
# Users can see who else is on the system, set a display name, choose whether to allow pokes,
# and then poke each other via chat ("poke mic") or the cockpit button (hold to poke, 👆🪿).

class ProfileUpdate(BaseModel):
    display_name: str = ""
    allow_poke: bool | None = None


@app.put("/account/profile")
def account_update_profile(req: ProfileUpdate, request: Request) -> JSONResponse:
    """Set display name and poke preference. Requires an authenticated session."""
    if not _caller_is_authenticated(request):
        return JSONResponse(status_code=401, content={"ok": False, "error": "sign in first"})
    from .session_auth import session_user, SESSION_COOKIE
    from .services.auth_accounts import update_profile
    uid = session_user(request.cookies.get(SESSION_COOKIE))
    if not uid:
        return JSONResponse(status_code=401, content={"ok": False, "error": "account session required"})
    res = update_profile(uid,
                         display_name=req.display_name or None,
                         allow_poke=req.allow_poke)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return JSONResponse(content=res)


@app.get("/users")
def list_users_route(request: Request) -> dict:
    """List all users with their display names and poke preferences. Auth required."""
    if not _caller_is_authenticated(request):
        raise HTTPException(status_code=401, detail="sign in first")
    from .session_auth import session_user, SESSION_COOKIE
    from .services.auth_accounts import list_users
    uid = session_user(request.cookies.get(SESSION_COOKIE)) or ""
    return {"ok": True, "users": list_users(exclude_user_id=uid)}


# ── Web Push: real PWA notifications (no third-party app) ───────────────────────────────────────
# These let an installed phone PWA receive push notifications even when closed. Requires HTTPS on
# the subscribing page (a browser rule). The public VAPID key is safe to expose; the private key
# never leaves the server. Subscriptions are device-token gated so only paired devices can register.

@app.get("/push/vapid-key")
def push_vapid_key() -> dict:
    """The VAPID public key the browser needs to subscribe. Public by design (it's a public key)."""
    from .services.web_push import public_key
    pk = public_key()
    if not pk:
        return {"ok": False, "error": "could not generate VAPID keys"}
    return {"ok": True, "public_key": pk}


@app.post("/push/subscribe")
async def push_subscribe(request: Request) -> JSONResponse:
    """Store a browser PushSubscription. Body: the PushSubscription JSON.

    Public-prefix but not unauthenticated: a subscription is accepted only from
    loopback/API-token/cookie-authenticated callers or from a valid paired-device
    token. Account sessions are bound to push_subscriptions.user_id so personal
    notifications can be delivered to the right person instead of broadcast to
    every installed PWA.
    """
    from .services.web_push import save_subscription
    from .session_auth import session_user, SESSION_COOKIE
    try:
        body = await request.json()
    except Exception:
        # FIX (external audit, additional findings #10): a malformed body is an ordinary bad
        # request, not a transient server outage. Unguarded, this fell through to the generic
        # exception handler, which returned a 503 "retry in a moment" (teaching the caller to
        # retry a request shape that will never succeed) and logged a full JSON-decode
        # traceback for what is a routine, expected-to-happen input error.
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_json",
                            "message": "the request body must be valid JSON"})
    sub = body.get("subscription") or body
    label = body.get("device_label", "") or ""

    uid = session_user(request.cookies.get(SESSION_COOKIE))
    account_user_id = uid if isinstance(uid, str) and uid else ""

    device_token = request.headers.get("x-mongoose-device-token", "")
    device_name = ""
    device_id = ""
    device_profile = ""
    if device_token:
        try:
            from .devices import device_for_token
            dev = device_for_token(device_token)
            if dev:
                device_name = str(dev.get("name") or "device")
                device_id = str(dev.get("device_id") or "")
                device_profile = str(dev.get("profile") or "")
        except Exception:
            device_name = ""
            device_id = ""
            device_profile = ""

    # No session cookie? Bind by DEVICE: whoever is on this paired device is that person, so a poke
    # to them actually reaches this phone. Without this, a subscription saved with no user_id was
    # invisible to personal (poke) delivery, which is exactly why pokes went nowhere.
    if not account_user_id and device_id:
        try:
            from .services.auth_accounts import account_for_device
            account_user_id = account_for_device(device_id) or ""
        except Exception:
            pass

    if not (account_user_id or device_name or _caller_is_authenticated(request)):
        return JSONResponse(status_code=401, content={"ok": False, "error": "sign in or pair this device first"})

    # Prevent the mis-scope that happened to Sam's iPhone: a guest subscribed without a device token, so
    # device_profile was blank and the subscription looked owner-level (it then received owner broadcasts
    # AND iOS silently dropped them). If we have no profile but the account is a KNOWN NON-OWNER, tag it
    # as a trusted_guest so owner/system broadcasts never reach it, however it subscribed.
    if not device_profile and account_user_id:
        try:
            from .services.auth_accounts import owner_account
            _owner = (owner_account() or {}).get("user_id") or ""
            if _owner and account_user_id != _owner:
                device_profile = "trusted_guest"
        except Exception:
            pass

    res = save_subscription(
        sub,
        user_id=account_user_id,
        device_label=label or device_name or ("account" if account_user_id else "cockpit"),
        device_id=device_id,
        device_profile=device_profile,
    )
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return JSONResponse(content=res)


@app.post("/push/unsubscribe")
async def push_unsubscribe(request: Request) -> JSONResponse:
    """Remove a stored subscription by endpoint. A device/account can remove its OWN
    subscription; a genuinely privileged owner surface (loopback/API-token/primary_phone) can
    remove any endpoint, matching subscribe's trust tiers. FIX (external audit, additional
    findings #7): this used to have NO in-route auth at all, and sat behind the outer
    localhost-only gate, so a guest who could subscribe could not unsubscribe (the PWA's
    disablePush() silently swallowed the 403 and told the guest notifications were off, while
    the server kept the endpoint live). Now symmetric with /push/subscribe."""
    from .services.web_push import remove_subscription, owners_for_endpoint
    from .session_auth import session_user, SESSION_COOKIE
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_json",
                            "message": "the request body must be valid JSON"})
    endpoint = body.get("endpoint", "")
    if not endpoint:
        return JSONResponse(status_code=400, content={"ok": False, "error": "endpoint required"})

    uid = session_user(request.cookies.get(SESSION_COOKIE))
    account_user_id = uid if isinstance(uid, str) and uid else ""
    device_token = request.headers.get("x-mongoose-device-token", "")
    device_id = ""
    is_owner_device = False
    if device_token:
        try:
            from .devices import device_for_token
            dev = device_for_token(device_token)
            if dev:
                device_id = str(dev.get("device_id") or "")
                is_owner_device = str(dev.get("profile") or "") == "primary_phone"
        except Exception:
            device_id = ""
    # "Privileged" here must mean OWNER-tier specifically: the Mac itself, the master API
    # token, or the owner's own paired phone. Re-scrutinising this fix caught a real bug:
    # _caller_is_authenticated() also returns True for ANY valid session cookie, and its own
    # docstring says it exists to gate account CREATION, a broader, different contract. Using
    # it here would have let any ordinary logged-in account (not just the owner) remove any
    # OTHER account's or guest's subscription. Composed narrowly instead of reusing that
    # helper, on purpose.
    client_host = request.client.host if request.client else ""
    is_loopback = client_host in _LOOPBACK_HOSTS
    api_token = getattr(get_settings(), "api_token", "") or ""
    header_tok = (request.headers.get("x-api-token", "")
                 or request.headers.get("x-mongoose-token", "")
                 or request.headers.get("authorization", "")).replace("Bearer ", "").strip()
    has_master_token = bool(api_token and header_tok and _secrets.compare_digest(header_tok, api_token))
    privileged = is_owner_device or is_loopback or has_master_token
    if not (account_user_id or device_id or privileged):
        return JSONResponse(status_code=401, content={"ok": False, "error": "sign in or pair this device first"})

    if not privileged:
        # Not a privileged owner surface: only remove an endpoint that actually belongs to
        # THIS caller's own identity, never someone else's just because the endpoint string
        # was guessed or observed.
        # Look the endpoint up UNSCOPED (by endpoint directly), not via list_subscriptions, which omits
        # device-scoped subs and so could not see a guest's/another device's sub, silently bypassing this
        # ownership gate (external audit finding #7, regressed: any caller could delete anyone's sub).
        existing = owners_for_endpoint(endpoint)
        owns_it = any(
            (account_user_id and s.get("user_id") == account_user_id)
            or (device_id and s.get("device_id") == device_id)
            for s in existing
        )
        if existing and not owns_it:
            return JSONResponse(status_code=403, content={"ok": False, "error": "not your subscription"})
    return JSONResponse(content=remove_subscription(endpoint))


@app.post("/push/test")
async def push_test(request: Request) -> JSONResponse:
    """Send a test push to all subscriptions, so the user can confirm notifications work. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services.web_push import send_web_push, record_test_result
    res = await send_web_push("Mongoose", "Push notifications are working! 🪿",
                              url="/device/launchpad", tag="test")
    try:
        record_test_result(int(res.get("sent", 0) or 0))
    except Exception:
        pass
    return JSONResponse(content=res)


@app.get("/cockpit/push/status")
def cockpit_web_push_status(request: Request) -> dict:
    """How many devices are subscribed to Web Push, and whether VAPID keys exist. Nonce-gated.
    (Distinct from /cockpit/push-status, which reports Docker/ntfy readiness for the Setup page.)"""
    _require_cockpit_nonce(request)
    from .services.web_push import subscription_count, public_key, list_subscriptions, push_state_summary
    summary = push_state_summary()
    return {"ok": True, "subscriptions": subscription_count(),
            "vapid_ready": bool(public_key()),
            "settings_saved": summary["settings_saved"],
            "last_test_ok_at": summary["last_test_ok_at"],
            "last_test_at": summary["last_test_at"],
            "devices": [{"label": s.get("device_label"), "profile": s.get("device_profile"),
                         "device_id": s.get("device_id"), "created_at": s.get("created_at"),
                         "last_ok_at": s.get("last_ok_at")} for s in list_subscriptions()]}


@app.post("/poke/{target}")
async def poke_user(target: str, request: Request) -> JSONResponse:
    """Poke another user by username or display name. Sends a 👆🪿 push notification.
    Requires an authenticated account session (bearer/local sessions cannot poke: pokes are personal)."""
    from .session_auth import session_user, SESSION_COOKIE
    from .services.auth_accounts import find_user, get_account, list_users
    from .services.poke_service import send_poke
    uid = session_user(request.cookies.get(SESSION_COOKIE))
    if not uid:
        return JSONResponse(status_code=401, content={"ok": False,
                            "error": "sign in with a username to poke people"})
    from_acct = get_account(uid)
    if not from_acct:
        return JSONResponse(status_code=401, content={"ok": False, "error": "account not found"})
    target_user = find_user(target)
    if not target_user:
        all_users = list_users(exclude_user_id=uid)
        names = [u["display_name"] for u in all_users]
        return JSONResponse(status_code=404, content={"ok": False,
                            "error": f"no user named '{target}'",
                            "available": names})
    if target_user["user_id"] == uid:
        return JSONResponse(status_code=400, content={"ok": False,
                            "error": "you cannot poke yourself (tempting as that is)"})
    if not target_user["allow_poke"]:
        return JSONResponse(status_code=403, content={"ok": False,
                            "error": f"{target_user['display_name']} has pokes turned off"})
    from_name = from_acct.get("display_name") or from_acct.get("username") or "someone"
    # Need display_name from the accounts service directly
    from .services.auth_accounts import find_user as _fu
    from_full = _fu(from_acct.get("username", ""))
    if from_full:
        from_name = from_full.get("display_name") or from_name
    result = await send_poke(uid, from_name, target_user["user_id"], target_user["display_name"])
    return JSONResponse(content=result)


@app.get("/poke/history")
def poke_history_route(request: Request) -> dict:
    """Recent pokes sent and received for the current user, each labelled with direction so a client can
    show 'pokes you got' without knowing its own user id."""
    from .session_auth import session_user, SESSION_COOKIE
    from .services.poke_service import poke_history
    uid = session_user(request.cookies.get(SESSION_COOKIE))
    if not uid:
        raise HTTPException(status_code=401, detail="account session required")
    pokes = poke_history(uid, limit=30)
    for p in pokes:
        p["direction"] = "received" if p.get("to_uid") == uid else "sent"
    received = [p for p in pokes if p["direction"] == "received"]
    return {"ok": True, "pokes": pokes, "received": received}


# ── Song exchange: weekly user-to-user recommendations with feedback loop ──────────────────────

class SongRecommendRequest(BaseModel):
    song: str
    recipient_name: str = ""  # used to resolve the exchange if exchange_id not supplied


class FeedbackRequest(BaseModel):
    feedback: str


@app.post("/song-exchange/{exchange_id}/recommend")
async def song_exchange_recommend(exchange_id: str, req: SongRecommendRequest,
                                   request: Request) -> JSONResponse:
    """Record a song recommendation for an open exchange.

    This is account-personal: only the user who was asked to recommend may
    complete the recommendation. A loopback/API-token caller is not enough,
    because otherwise any owner/browser process could act as either participant.
    """
    from .session_auth import session_user, SESSION_COOKIE
    uid = session_user(request.cookies.get(SESSION_COOKIE))
    if not uid:
        return JSONResponse(status_code=401, content={"ok": False, "error": "account session required"})
    from .services.song_exchange import record_recommendation
    res = await record_recommendation(exchange_id, req.song, actor_user_id=uid)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return JSONResponse(content=res)


@app.post("/song-exchange/{exchange_id}/feedback")
async def song_exchange_feedback(exchange_id: str, req: FeedbackRequest,
                                  request: Request) -> JSONResponse:
    """Record feedback on a song recommendation. Only the recipient account may answer."""
    from .session_auth import session_user, SESSION_COOKIE
    uid = session_user(request.cookies.get(SESSION_COOKIE))
    if not uid:
        return JSONResponse(status_code=401, content={"ok": False, "error": "account session required"})
    from .services.song_exchange import record_feedback
    res = await record_feedback(exchange_id, req.feedback, actor_user_id=uid)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return JSONResponse(content=res)


@app.get("/song-exchange/history")
def song_exchange_history(request: Request) -> dict:
    """Recent song exchanges for the current user."""
    from .session_auth import session_user, SESSION_COOKIE
    from .services.song_exchange import list_exchanges
    uid = session_user(request.cookies.get(SESSION_COOKIE))
    if not uid:
        raise HTTPException(status_code=401, detail="account session required")
    return {"ok": True, "exchanges": list_exchanges(uid, limit=20)}

class MusicAnswer(BaseModel):
    facet: str
    value: str


@app.get("/cockpit/music")
def cockpit_music() -> dict:
    """The owner's learned music profile, whether proactive music has been turned off (opted_out),
    whether Mongoose is still in his eager get-to-know-you phase, and the next question he'd like to
    ask. Proactive music is ON by default; opted_out is the suppression flag. Read-only."""
    from .services import music_taste as MT
    prof = MT.profile(MT.OWNER_SCOPE)
    nxt = MT.next_unasked_facet(MT.OWNER_SCOPE)
    return {"ok": True, "profile": prof["facets"], "onboarded": prof["onboarded"],
            "opted_in": MT.has_opted_in(MT.OWNER_SCOPE),
            "opted_out": MT.is_opted_out(MT.OWNER_SCOPE),
            "is_onboarding": MT.is_onboarding(MT.OWNER_SCOPE),
            "next_question": (MT.question_for(nxt) if nxt else None),
            "next_facet": nxt, "summary": MT.profile_summary(MT.OWNER_SCOPE)}


@app.post("/cockpit/music/opt")
def cockpit_music_opt(request: Request, enabled: bool = True) -> JSONResponse:
    """Turn Mongoose's proactive music on or off. Proactive music is ON by default (it's part of his
    character); enabled=false opts out and stops proactive nudges, enabled=true clears that opt-out.
    Chat-time music curiosity is unaffected either way."""
    if not _caller_is_authenticated(request):
        return JSONResponse(status_code=401, content={"ok": False, "error": "sign in first"})
    from .services import music_taste as MT
    if enabled:
        MT.opt_in(MT.OWNER_SCOPE)
    else:
        MT.opt_out(MT.OWNER_SCOPE)
    return JSONResponse(content={"ok": True, "opted_out": MT.is_opted_out(MT.OWNER_SCOPE)})


@app.post("/cockpit/music/answer")
def cockpit_music_answer(ans: MusicAnswer, request: Request) -> JSONResponse:
    """Record what the owner told Mongoose about a music facet (singer/band/genre/decade/etc.). Answering
    a music question also opts the owner into the music chat (it's an explicit invite)."""
    if not _caller_is_authenticated(request):
        return JSONResponse(status_code=401, content={"ok": False, "error": "sign in first"})
    from .services import music_taste as MT
    res = MT.record_answer(ans.facet, ans.value, MT.OWNER_SCOPE)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return JSONResponse(content=res)


@app.get("/cockpit/music/recommend")
def cockpit_music_recommend(song: str = "") -> dict:
    """A quirky, freshly-phrased music nudge. Pass ?song=Title by Artist to phrase a suggestion for that
    song; omit it and Mongoose just asks an open music question. He never invents a fake song himself."""
    from .services import music_taste as MT
    return {"ok": True, "text": MT.recommendation_text(song, scope=MT.OWNER_SCOPE)}


@app.get("/cockpit/lock/status")
def cockpit_lock_status() -> dict:
    """Whether the cockpit requires a login even on the Mac (lock_local), and the session TTLs, so the
    UI can show an honest lock/timeout state. Read-only."""
    from .session_auth import _DEFAULT_TTL, _REMEMBER_TTL
    s = get_settings()
    return {"lock_local": bool(getattr(s, "lock_local", False)),
            "session_ttl_hours": _DEFAULT_TTL // 3600,
            "remember_ttl_days": _REMEMBER_TTL // 86400,
            "has_token": bool(getattr(s, "api_token", ""))}


@app.post("/cockpit/lock/set")
async def cockpit_lock_set(request: Request) -> dict:
    """Turn the loopback lock on/off (persists MONGOOSE_LOCK_LOCAL to .env). Nonce-gated. Enabling it
    without an API token set would lock you out, so refuse that and explain."""
    _require_cockpit_nonce(request)
    body = await request.json()
    want = bool(body.get("lock_local"))
    s = get_settings()
    if want and not getattr(s, "api_token", ""):
        return {"ok": False, "error": "Set an API token first (Settings → remote access), "
                "otherwise enabling the lock would lock you out with no way to log in."}
    from .setup_wizard import write_env
    write_env({"MONGOOSE_LOCK_LOCAL": "true" if want else "false"})
    # settings is cached; reflect the change immediately for subsequent requests in this process.
    import mongoose.settings as _S
    if hasattr(_S, "_settings"):
        _S._settings = None
    if hasattr(_S.get_settings, "cache_clear"):
        _S.get_settings.cache_clear()
    return {"ok": True, "lock_local": want,
            "detail": "Cockpit now requires a login on every device." if want
                      else "Cockpit is open on this Mac (loopback) again."}


@app.get("/cockpit/whatsapp/state")
def cockpit_whatsapp_state(request: Request) -> dict:
    """WhatsApp connection state for BOTH accounts (the owner's inbox + Mongoose sender), so the cockpit
    can guide two QR links. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services.whatsapp_journey import accounts_state
    return accounts_state()


@app.post("/cockpit/whatsapp/connect")
async def cockpit_whatsapp_connect(request: Request) -> dict:
    """Start one account's WhatsApp session so it begins authenticating (then poll state for the QR).
    Body: {"role": "owner_read" | "mongoose_send"}. Nonce-gated."""
    _require_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services.whatsapp_journey import connect
    from .services.whatsapp_accounts import OWNER_READ
    # OFF the loop: connect makes a sync httpx call to the WAHA container (up to 10s).
    return await _asyncio.to_thread(connect, str(body.get("role") or OWNER_READ))


@app.post("/cockpit/whatsapp/start")
def cockpit_whatsapp_start(request: Request) -> dict:
    """Start the WAHA Docker sidecar so WhatsApp can be linked, without dropping to a terminal. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services.whatsapp_journey import start_container
    return start_container()


@app.get("/cockpit/whatsapp/qr")
def cockpit_whatsapp_qr(request: Request) -> dict:
    """The login QR (base64 PNG) for one account, to scan with WhatsApp. ?role=owner_read|mongoose_send.
    Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services.whatsapp_journey import login_qr
    from .services.whatsapp_accounts import OWNER_READ
    role = request.query_params.get("role") or OWNER_READ
    return login_qr(role)


@app.get("/cockpit/whatsapp/activity")
def cockpit_whatsapp_activity(request: Request) -> dict:
    """Recent WhatsApp messages + pending drafts for the monitoring surface. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services.whatsapp_journey import recent_activity
    return recent_activity()


@app.post("/cockpit/whatsapp/test")
async def cockpit_whatsapp_test(request: Request) -> dict:
    """Send a test message to your own number through the approved-outbox path. Nonce-gated."""
    _require_cockpit_nonce(request)
    body = await request.json()
    import asyncio as _asyncio
    from .services.whatsapp_journey import send_test
    # OFF the loop: send_test does sequential sync httpx calls to WAHA (status + send, up to ~20s).
    return await _asyncio.to_thread(send_test, str(body.get("to") or ""))


@app.get("/cockpit/what-changed")
def cockpit_what_changed() -> dict:
    """Deterministic cross-domain delta for the Activity tab. PEEKS (acknowledge=
    False), so merely viewing the cockpit does not reset your 'since last looked'
    checkpoint, that reset belongs to an explicit action, not a passive poll."""
    from .services.journal_layer import what_changed
    try:
        return what_changed(acknowledge=False)
    except Exception as exc:
        return {"changes": [], "detail": {}, "error": str(exc)}


@app.get("/cockpit/nonce")
def cockpit_nonce() -> dict:
    """Same-origin read of the cockpit action nonce, echoed back on privileged POSTs."""
    return {"cockpit_nonce": _COCKPIT_NONCE}


# Stale-tab guard: a per-process boot id plus a session epoch that advances every time a cockpit
# loads. A tab left open across an update/restart, or a second tab opened later, polls this; when
# the boot id changes or a newer epoch appears it knows it's been superseded and stops writing, so
# two clients don't fight over the same conversation (the "old tab kept writing" bug).
_SESSION_BOOT_ID = _secrets.token_urlsafe(8)
_SESSION_EPOCH = 0


@app.get("/cockpit/session/claim")
def cockpit_session_claim() -> dict:
    """A freshly-loaded cockpit claims the session, bumping the epoch. Returns the boot id and new
    epoch, which the tab then watches; any tab on an older epoch knows it's been superseded."""
    global _SESSION_EPOCH
    _SESSION_EPOCH += 1
    return {"boot_id": _SESSION_BOOT_ID, "epoch": _SESSION_EPOCH}


@app.get("/cockpit/session/current")
def cockpit_session_current() -> dict:
    """Current boot id + epoch, polled by open tabs to detect they've been superseded by a newer
    tab or a server restart/update."""
    return {"boot_id": _SESSION_BOOT_ID, "epoch": _SESSION_EPOCH}


@app.get("/cockpit/liveness")
def cockpit_liveness() -> dict:
    """Is the system fresh or quietly rotting? Stale sync / dead jobs / dropped sessions /
    aging tokens, each with a fix. The antidote to silent decay."""
    from .liveness import liveness
    # SWR-cached like readiness: liveness scans sync/jobs/sessions/tokens and is not cheap, and the phone
    # polls it on every cockpit open. Serve stale instantly, refresh in the background, so a poll never
    # blocks a worker for tens of seconds (which starved the event loop and dropped the tunnel origin).
    return _status_cached("liveness", 30.0, liveness)


@app.get("/cockpit/brain/wakeup")
def cockpit_brain_wakeup(request: Request) -> dict:
    """The brain's last wake-up self-check result (from startup), plus a fresh read-only re-check.
    Shows whether the brain came back with full working memory and what, if anything, was repaired."""
    _require_cockpit_nonce(request)
    from .brain_wakeup import wakeup_summary
    last = getattr(app.state, "brain_wakeup", None)
    return {"ok": True, "startup": last, "now": wakeup_summary()}


@app.post("/cockpit/brain/wakeup/repair")
def cockpit_brain_wakeup_repair(request: Request) -> dict:
    """Force a brain wake-up repair pass on demand (checkpoint WAL, recreate missing tables,
    rebuild desynced FTS). Nonce-gated."""
    _require_cockpit_nonce(request)
    from .brain_wakeup import wake_up
    return wake_up(repair=True)


@app.get("/cockpit/system-map")
def cockpit_system_map() -> dict:
    """What Mongoose IS: what's connected, what each surface can see, where data lives, and
    what (if anything) leaves this machine. The transparency counterpart to the receipts
    ledger ('what Mongoose did'). Includes the running version/build so the System tab can show it."""
    from .transparency import system_map
    from . import __version__
    try:
        from . import __codename__ as _cn
    except Exception:
        _cn = ""
    m = system_map()
    m["version"] = __version__
    m["codename"] = _cn
    m["release"] = (f"{__version__} ({_cn})" if _cn else __version__)
    return m


@app.post("/cockpit/onboarding/skip-connect")
def cockpit_onboarding_skip_connect(request: Request) -> dict:
    """Persist the 'I'll connect data later' choice so first-run resumes at the phone step
    instead of forgetting the choice on reload. Loopback/first-run only (it's part of the
    pre-auth front door); a configured-token LAN client must be authenticated."""
    client_host = request.client.host if request.client else ""
    is_loopback = client_host in _LOOPBACK_HOSTS
    token = getattr(get_settings(), "api_token", "") or ""
    if not is_loopback and token:
        # require auth once a token is set
        from .session_auth import verify_session, SESSION_COOKIE
        if not verify_session(request.cookies.get(SESSION_COOKIE)):
            raise HTTPException(status_code=403, detail="authentication required")
    from .db import set_app_state
    set_app_state("onboarding_skip_connect", "true")
    return {"ok": True}


@app.get("/cockpit/onboarding")
def cockpit_onboarding(request: Request) -> dict:
    """Where the person is in first-run, and the single next action. Drives /welcome.

    Must stay reachable pre-auth (it's the front door before any token exists). But once an
    API token IS configured, a non-loopback, unauthenticated LAN client gets only a COARSE
    view (current step + overall progress), not the detailed per-source/per-device signals,
    those needn't be exposed to the whole LAN after setup. (QA-audit P1)
    """
    from .onboarding import onboarding_state
    state = onboarding_state()

    client_host = request.client.host if request.client else ""
    is_loopback = client_host in _LOOPBACK_HOSTS
    token = getattr(get_settings(), "api_token", "") or ""
    authed = False
    if not is_loopback and token:
        # same notion of "authenticated" the guard uses: header token or a valid session cookie
        from .session_auth import verify_session, SESSION_COOKIE
        header_tok = request.headers.get("x-api-token", "") or request.headers.get("authorization", "")
        if header_tok and _secrets.compare_digest(header_tok.replace("Bearer ", "").strip(), token):
            authed = True
        elif verify_session(request.cookies.get(SESSION_COOKIE)):
            authed = True

    # Full detail for loopback, for authenticated callers, or during tokenless first-run.
    if is_loopback or authed or not token:
        return state
    # Coarse public view: enough for a generic "is it set up yet?" without leaking specifics.
    return {
        "current_step": state.get("current_step"),
        "complete": state.get("complete"),
        "progress_pct": state.get("progress_pct"),
    }


@app.get("/welcome", response_class=HTMLResponse)
def welcome_page() -> HTMLResponse:
    """The guided first-run flow: one path from 'the app just opened' to 'my phone is
    paired'. The phone handoff is the closing step of THIS flow, not a separate tab. This
    is the front door a fresh install lands on. Served no-cache (see /ui)."""
    from .cockpit import welcome_html
    return HTMLResponse(welcome_html(), headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0"})


@app.get("/install", response_class=HTMLResponse)
def install_page() -> HTMLResponse:
    """The live install progress screen. Opened by the installer; polls /install/progress and
    ends by showing the phone pairing QR. Public: it runs before any auth exists. Served no-store
    (like /ui and /welcome): this page is the MOST likely to be reopened across installs, so a
    cached copy is exactly the "I opened a previous /install" bug. Force a fresh fetch every time.

    Clear-Site-Data is also sent: a standards-based instruction the browser acts on the instant
    it processes THIS response, wiping stored state for the origin, including unregistering any
    service worker, without the person doing it by hand in a Develop menu. It cannot reach past
    an ALREADY-intercepting stale worker (that worker fabricates its own response and this one
    never arrives), but the moment any request for /install gets through to the real server, the
    browser is told to make sure nothing stale can intercept the next one."""
    from .cockpit import install_html
    return HTMLResponse(install_html(), headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0",
        "Clear-Site-Data": '"cache", "storage"'})


@app.get("/install/progress")
def install_progress_endpoint() -> dict:
    """Live per-step install progress for the /install screen."""
    from .install_progress import install_progress
    return install_progress()


@app.get("/cockpit/tls/status")
def cockpit_tls_status(request: Request) -> dict:
    """Guest-HTTPS readiness: certificate presence/expiry + Let's Encrypt prerequisites + the
    one-click self-signed option (the zero-setup path that unlocks phone voice on the LAN). Nonce-gated."""
    _require_cockpit_nonce(request)
    from .acme_tls import certificate_status, preflight
    from .self_signed_tls import self_signed_status
    return {"certificate": certificate_status(), "preflight": preflight(),
            "self_signed": self_signed_status()}


@app.post("/device/remote/repair")
async def device_remote_repair(request: Request) -> dict:
    """Owner-device action: work through the real chain of things that can make the DDNS
    route fail. See _run_remote_repair_chain for the actual sequence; this route only adds
    the device-token profile check (a guest was never given this address and has no reason
    to poke at the router). The cockpit-nonce-gated /cockpit/remote-access/repair runs the
    exact same chain for the owner's own desktop session."""
    dev = _device_from_request(request)
    profile = str((dev or {}).get("profile") or "")
    if profile not in {"primary_phone", "home_tablet"}:
        raise HTTPException(status_code=403, detail="remote route repair is owner-device only")
    return await _run_remote_repair_chain()


async def _run_remote_repair_chain() -> dict:
    """The real chain of things that can make the DDNS route fail, in order, cheapest and
    most Mongoose-side first, most invasive last.

    1. Is Mongoose's OWN listener even accepting connections, independent of DNS/router?
       If not, that alone explains ERR_CONNECTION_REFUSED regardless of how well the router
       is configured, and the fix is a restart (the same one already used for the LAN case).
    2. Refresh DDNS and re-run the external reachability diagnosis.
    3. Only if DDNS is genuinely configured (an existing, deliberate signal of the owner's
       intent to be reachable from outside) AND the diagnosis specifically says the router
       side looks like a missing port-forward: attempt ONE bounded UPnP mapping for the
       single port Mongoose actually uses. This is owner-triggered by this exact tap, never
       a silent background/scheduler action, and the result (method used, success or not)
       is always returned plainly, never hidden.

    Callers are responsible for their own authorization; this function assumes the caller
    is already established as the owner. Never requests more than the one port Mongoose
    uses; never attempts NAT-PMP or UPnP unless DDNS is already configured and nothing
    simpler already works."""
    try:
        from .ddns import update_now, reachability_diagnosis, public_listener_acceptance, attempt_upnp_repair
        from .setup_wizard import effective_env

        listener = public_listener_acceptance()
        if not listener.get("ok"):
            restart = _schedule_detached_phone_restart()
            return {"ok": False, "step": "listener", "listener": listener, "restart": restart,
                    "detail": ("Mongoose's own listener wasn't accepting connections "
                              f"({listener.get('reason')}); no router or DDNS setting can fix "
                              "that. Restarting Mongoose now so it comes up correctly; try "
                              "again in a few seconds.")}

        upd = update_now()
        env = effective_env()
        diag = reachability_diagnosis(env)
        if diag.get("ok"):
            return {"ok": True, "step": "reachable", "update": upd, "diagnosis": diag,
                    "detail": "DDNS is up to date and this Mac can reach its own public address."}
        if diag.get("reason") == "dns_stale":
            return {"ok": False, "step": "dns_stale", "update": upd, "diagnosis": diag,
                    "detail": "Updated DDNS just now; it can take a few minutes to propagate. Try again shortly."}
        if diag.get("reason") == "hairpin_or_no_portforward":
            has_ddns = bool(env.get("DDNS_PROVIDER"))
            if not has_ddns:
                return {"ok": False, "step": "no_ddns", "diagnosis": diag,
                        "detail": "Set up Dynamic DNS first; there's no away-from-home address to repair yet."}
            port = int(getattr(get_settings(), "port", 8765) or 8765)
            upnp = attempt_upnp_repair(internal_port=port)
            if upnp.get("ok"):
                diag2 = reachability_diagnosis(env)
                return {"ok": bool(diag2.get("ok")), "step": "upnp_attempted", "upnp": upnp,
                        "diagnosis": diag2,
                        "detail": ("Asked your router to forward this port via UPnP; it accepted the "
                                  "request. " + ("Confirmed reachable now." if diag2.get("ok") else
                                  "Give it a moment and try Open Mongoose again."))}
            return {"ok": False, "step": "upnp_failed", "upnp": upnp, "diagnosis": diag,
                    "detail": ("DDNS and this Mac's settings are correct, and Mongoose tried to ask "
                              "your router to forward this port automatically (UPnP), but that "
                              f"didn't succeed ({upnp.get('error', 'no response')}). Either your "
                              "router doesn't support UPnP, has it turned off, or the port needs a "
                              "manual forward in your router's settings. If you're on the same "
                              "Wi-Fi as this Mac, this link may also simply not work from here even "
                              "once the forward exists (a separate router limitation); try it from "
                              "cellular data instead.")}
        return {"ok": False, "step": "unverified", "update": upd, "diagnosis": diag,
                "detail": "Could not confirm reachability from here."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300],
                "detail": "Could not run the DDNS repair check."}


@app.post("/cockpit/remote-access/repair")
async def cockpit_remote_repair(request: Request) -> dict:
    """The desktop-Cockpit equivalent of /device/remote/repair: runs the exact same repair
    chain, gated by the cockpit nonce (already owner-only) instead of a device-token profile
    check. Exists because the phone route requires a PAIRED PHONE, which is not how someone
    sitting at the Mac's own Cockpit reaches this."""
    _require_cockpit_nonce(request)
    return await _run_remote_repair_chain()


@app.get("/cockpit/tunnel/state")
async def cockpit_tunnel_state(request: Request) -> dict:
    """Everything the tunnel card needs in one call: each setup fact independently checked,
    plus live end-to-end proof when configured (cheap enough to run on card refresh, and
    it's the only status that actually means 'guests can reach this')."""
    _require_cockpit_nonce(request)
    from .cloudflare_tunnel import tunnel_state, tunnel_end_to_end_proof
    st = tunnel_state()
    proof = tunnel_end_to_end_proof(st["hostname"]) if st["configured"] else None
    return {"ok": True, "state": st, "proof": proof}


@app.post("/cockpit/tunnel/install")
async def cockpit_tunnel_install(request: Request) -> dict:
    """Download the official cloudflared binary into Mongoose's own data/bin. No Homebrew,
    no sudo, no terminal; 'install' for a single static Go binary is just a download."""
    _require_cockpit_nonce(request)
    from .cloudflare_tunnel import install_cloudflared
    return install_cloudflared()


@app.post("/cockpit/tunnel/login")
async def cockpit_tunnel_login(request: Request) -> dict:
    """Start the Cloudflare login server-side and hand the UI the approval URL as a
    clickable link. The browser approval itself is the one human step in the journey:
    it's Cloudflare's own account auth, deliberately not automatable."""
    _require_cockpit_nonce(request)
    from .cloudflare_tunnel import start_login
    return start_login()


@app.post("/cockpit/tunnel/provision")
async def cockpit_tunnel_provision(request: Request) -> dict:
    """One button after login: create the tunnel, route the typed hostname to it, write
    the ingress config at this Mongoose, start the supervised process, persist the env
    keys. Reports per-step so a failure names exactly where it stopped."""
    _require_cockpit_nonce(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    from .cloudflare_tunnel import provision
    return provision(str(payload.get("hostname") or ""))


@app.post("/cockpit/service/install")
async def cockpit_service_install(request: Request) -> dict:
    """Install and load the user-level server-watchdog LaunchAgent (no sudo) so Mongoose,
    and with it the supervised tunnel, actually returns after a Mac reboot.

    Deliberately NOT service.install(): checked before wiring, that one's plist runs
    `mongoose scheduler`, which never starts the server; a button labelled 'start Mongoose
    after reboot' backed by it would be exactly the live-looking-but-wrong wiring this
    session has been unpicking all night. The watchdog agent (RunAtLoad + KeepAlive) keeps
    the watchdog alive, and the watchdog keeps the SERVER alive, which is the actual
    requirement."""
    _require_cockpit_nonce(request)
    try:
        from .cli import _wd_install_agent
        import subprocess as _sp
        plist = _wd_install_agent()
        _sp.run(["launchctl", "unload", plist], capture_output=True, timeout=15)
        r = _sp.run(["launchctl", "load", plist], capture_output=True, text=True, timeout=15)
        # Report the re-read FACT, not launchctl's return code: the whole point of
        # reboot_persistent is that claims come from independent verification.
        from .cloudflare_tunnel import _reboot_persistent
        ok = _reboot_persistent()
        return {"ok": ok, "plist": plist,
                "detail": ("Loaded and verified. Mongoose will start automatically after a reboot." if ok
                          else (r.stderr or r.stdout or "loaded, but verification still reports not persistent")[:300])}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


@app.post("/cockpit/tls/acme-obtain")
async def cockpit_tls_acme_obtain(request: Request) -> dict:
    """Get a real trusted Let's Encrypt certificate for the public DuckDNS host, one click, no
    terminal. obtain_certificate() already existed and did the real work (DNS-01 via certbot)
    but nothing ever called it; guest-invite copy previously pointed people at a terminal
    command instead. Runs in a background thread since certbot's DNS challenge can take
    minutes; check /cockpit/tls/status afterward for the result."""
    _require_cockpit_nonce(request)
    import threading
    from .acme_tls import preflight, obtain_certificate
    pf = preflight()
    if not pf.get("ok"):
        return {"ok": False, "error": "; ".join(pf.get("problems", [])), "preflight": pf}
    threading.Thread(target=obtain_certificate, daemon=True).start()
    return {"ok": True, "message": "Requesting your certificate now; this can take a minute or "
                                    "two. Check back here or on Remote access for the result."}


@app.post("/cockpit/tls/self-signed")
def cockpit_tls_self_signed(request: Request) -> JSONResponse:
    """Generate a self-signed certificate in-process (no openssl/mkcert/certbot needed) covering
    localhost, this Mac's LAN IP, and mongoose.local, then point .env at it. One click; the user
    restarts Mongoose and gets HTTPS, which is what unlocks the microphone on phones. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .self_signed_tls import generate_self_signed
    res = generate_self_signed()
    if not res.get("ok"):
        return JSONResponse(status_code=500, content=res)
    return JSONResponse(content=res)


@app.get("/install/owner-secret")
def install_owner_secret(request: Request) -> dict:
    """The owner-pairing secret, returned ONLY to a loopback caller (the Mac itself). This is how the
    /install page shows the owner the secret to type on their phone, without ever exposing it to the network.
    Remote callers get nothing."""
    client_host = request.client.host if request.client else ""
    if client_host not in _LOOPBACK_HOSTS:
        raise HTTPException(status_code=403, detail="local only")
    from .onboarding import _paired_count
    if _paired_count() > 0:
        return {"active": False}  # owner already paired; secret irrelevant
    from .owner_pairing import get_or_create_owner_secret, owner_secret_active
    if not owner_secret_active():
        return {"active": False}
    return {"active": True, "secret": get_or_create_owner_secret()}


@app.post("/install/pair-code")
def install_pair_code(request: Request) -> dict:
    """Mint a pairing code + QR for the END of the install flow, so the install literally
    ends at 'scan this with your phone'. Allowed pre-auth ONLY while install just finished and
    no devices are paired yet (a narrow, first-run-only window); once anything is paired or a
    token is configured for a remote caller, fall back to the authenticated cockpit pairing."""
    from .install_progress import install_progress as _ip
    from .onboarding import _paired_count
    client_host = request.client.host if request.client else ""
    is_loopback = client_host in _LOOPBACK_HOSTS
    # Gate: only during the first-run install window (install ready, nothing paired yet), and
    # for remote callers only when no token is set (tokenless first run). Loopback always ok
    # in this window since the installer opens the page locally.
    ready = _ip().get("ready")
    paired = _paired_count()
    # Gate: only during the first-run install window (install ready, nothing paired yet).
    # IMPORTANT: after `enable-phone` writes a token, we must STILL allow the first pairing
    # (that's the whole point of enable-phone during install). The old gate blocked any remote
    # caller with a token, which made enable-phone break the very pairing it was enabling.
    # Rule: allow if nothing paired yet AND (loopback OR install just completed).
    if paired > 0:
        raise HTTPException(status_code=403, detail="pairing is available from the cockpit "
                            "Devices tab once setup is complete")
    if not is_loopback and not ready:
        raise HTTPException(status_code=409, detail="install not finished")
    # Owner-identity gate: claiming the OWNER slot (primary_phone) requires the owner secret shown
    # only on the Mac (install terminal + the loopback /install page). This stops a guest or any
    # device on the network from grabbing the owner slot just by scanning first. The secret is
    # accepted from the request body ("owner_secret") or a header. A loopback caller (the Mac itself)
    # is trusted without it, since physical access to the Mac is the same trust level the secret
    # represents.
    from .owner_pairing import verify_owner_secret, get_or_create_owner_secret
    # Read the presented secret from query or header.
    presented = (request.query_params.get("owner_secret")
                 or request.headers.get("X-Owner-Secret") or "")
    # CRITICAL FIX (external audit, additional findings #1): the old check was
    # `if owner_secret_active() and not is_loopback and ...`, i.e. it only enforced the
    # secret if one ALREADY existed on disk. On a genuinely fresh install nothing has ever
    # minted one, so owner_secret_active() was False and the entire condition short-circuited
    # away, letting the FIRST remote caller through with no check at all, straight to a
    # primary_phone token: full owner device capabilities, before the real owner ever pairs.
    # For any non-loopback caller we must now ENSURE the secret exists (minting one if this
    # is genuinely the first attempt) BEFORE checking whether the presented value matches it,
    # so a remote caller can never win a race against the secret's own creation. A caller who
    # has never seen the Mac's own /install page or terminal cannot possibly present the right
    # value, so this correctly 401s the very first remote attempt too, not just the second.
    if not is_loopback:
        get_or_create_owner_secret()
        if not verify_owner_secret(presented):
            # Defense in depth beyond the secret's own entropy (33^6 =~ 1.3 billion
            # combinations): that keyspace assumes a human typing a code shown once on the
            # Mac, not an unthrottled script grinding through guesses over the LAN with no
            # per-request latency. Reuse the same per-source throttle built for the poke/
            # mailbox hardening earlier this session, keyed by the caller's IP since there is
            # no paired-device identity yet at this pre-auth stage.
            limited = _rate_limited(
                "owner_secret_attempt", client_host, cooldown_s=3.0, window_s=60.0,
                max_per_window=5,
                cooldown_msg="too many attempts, wait a few seconds and try again",
                cap_msg="too many attempts from this address, slow down")
            if limited:
                raise HTTPException(status_code=429, detail=limited)
            raise HTTPException(status_code=401, detail="owner_secret_required")
    from .devices import create_pairing_code, pairing_qr_svg
    result = create_pairing_code("primary_phone")
    # Owner device is being paired now; consume the one-time secret so it doesn't linger.
    from .owner_pairing import consume_owner_secret
    consume_owner_secret()
    scheme = "https" if request.url.scheme == "https" else "http"
    # REGRESSION FIX: this used to prefer LAN-first for the owner's own phone, then got
    # changed to prefer DDNS unconditionally ("so the QR works even if scanned from
    # outside home" / "you might scan from the office"). But the overwhelmingly common case
    # for primary_phone pairing is standing right next to the Mac, phone on the SAME WiFi,
    # about to scan immediately. Most consumer routers do not support NAT loopback/hairpin
    # routing, so a LAN device trying to reach the router's OWN public DDNS hostname from
    # inside the network gets a flat connection refusal, which is exactly what this looked
    # like. LAN-first restores the reliable common case; DDNS is offered as an explicit
    # fallback link in the response instead of silently taking over the primary QR.
    lan_h = _phone_reachable_host(request)
    lan_b = f"{scheme}://{lan_h}"
    ddns_b = _guest_reachable_url(request)
    pair_url = lan_b + f"/pair?code={result['code']}"
    result["pair_url"] = pair_url  # so the UI can show a typed fallback when the QR isn't available
    if ddns_b and ddns_b != lan_b:
        result["remote_pair_url"] = f"{ddns_b}/pair?code={result['code']}"
        result["remote_pair_hint"] = "pairing away from home? use this link instead"
    # Attach the machine-detected preflight so the UI can show honest candidate URLs + warnings (HTTP vs
    # HTTPS, loopback-only, NAT-loopback caveat) instead of the user guessing why the phone can't connect.
    try:
        from .services.pairing_preflight import preflight as _pf
        result["preflight"] = _pf(verify_local=False)
    except Exception:
        pass
    note = _pairing_reachability_note()
    if note:
        # Do not hand out a QR that already has a known reason to fail (external triage): a
        # scannable-looking code that leads to ERR_CONNECTION_REFUSED is worse than no code at
        # all, because it reads as confident and the phone just failed for "no reason".
        result["qr_svg"] = ""
        result["qr_blocked"] = True
        result["reachability_warning"] = note
        result["reachability_diagnosis"] = _phone_reachability_diagnosis()
        # DEFENSE IN DEPTH (external triage: "restriction vs restoration", blocking the QR was
        # necessary but not the actual fix; the product requirement is repairing the socket
        # path, not just refusing to lie about it). Only fires if the caller explicitly asks
        # via auto_repair, never as a silent side effect of merely requesting a pairing code.
        # This route is already owner-only (loopback or the owner secret), so the flag adds no
        # new reachable surface; it reuses ensure_phone_access_defaults(), the same function
        # the existing Enable-phone button already calls.
        auto = str(request.query_params.get("auto_repair") or "").lower() in ("1", "true", "yes", "on")
        if auto:
            result["auto_repair"] = _repair_phone_reachability("install_pair_code")
    else:
        result["qr_svg"] = pairing_qr_svg(pair_url)
    return result


@app.get("/cockpit/setup-state")
def cockpit_setup_state(request: Request) -> dict:
    """The canonical setup-state answer for every owner-facing surface, in one call.
    Overview, doctor, and any future screen should read from HERE rather than each computing
    its own version of "is HTTPS ready", "is the phone reachable", etc: that per-surface
    duplication is exactly what let Overview say "not configured, run make_cert.sh" next to a
    cockpit panel correctly showing a live, working certificate. Nonce-gated like the other
    cockpit-only reads."""
    _require_cockpit_nonce(request)
    from .setup_state import setup_state as _setup_state
    only = request.query_params.get("only")
    names = [n.strip() for n in only.split(",")] if only else None
    return _setup_state(only=names)


@app.get("/cockpit/readiness")
def cockpit_readiness() -> dict:
    """The full readiness graph for the cockpit 'What can I use now?' view: every source,
    surface and service with evidence, status, what it affects, and the next action. Read-only;
    no secrets (collectors return statuses + counts, never credential values)."""
    from .services.readiness import readiness_report
    return _status_cached("readiness", 45.0, readiness_report)




@app.get("/connectivity/context")
def connectivity_context_endpoint(request: Request) -> dict:
    """Read-only per-request diagnosis: loopback vs LAN/VPN vs public/DDNS/proxy, with warnings for
    common browser/phone session mistakes. Gated because it exposes local host/access details."""
    _require_token_or_cockpit_nonce(request)
    from .services.connectivity_context import describe_request_context
    return describe_request_context(request)


@app.get("/connectors/status")
async def connectors_status(only: str = "") -> dict:
    """Live connector HEALTH: actually probes each connector (Gmail, Calendar, Drive, Microsoft, IMAP,
    Home Assistant, Ollama) to prove it's USABLE right now, not merely configured. This is the
    'configured means usable' enforcement: a connector that's set up but whose token was revoked or
    whose host is unreachable shows as BROKEN, loudly, instead of a false green. Read-only, each probe
    is a single cheap call with a hard timeout. Runs off-thread so slow remotes can't block."""
    import asyncio as _asyncio
    from .connector_probes import probe_all
    names = [n.strip() for n in only.split(",") if n.strip()] or None
    if names is None:
        # The connectors tab asks for the full picture on every open; SWR-cache it (probes are now
        # concurrent, but still a live network round-trip each) so repeat loads are instant and a stale
        # value refreshes in the background. A targeted re-probe (?only=x) stays fully live.
        return await _status_cached_async(
            "connectors_status", 20.0, lambda: _asyncio.to_thread(probe_all))
    return await _asyncio.to_thread(probe_all, only=names)


@app.get("/connectors/probe/{name}")
async def connector_probe_one(name: str) -> dict:
    """Live-probe a single connector by name (ollama/home_assistant/gmail/google_calendar/google_drive/
    microsoft/imap/alexa_bridge). Returns whether it's genuinely usable right now, with evidence and
    latency. Read-only, off-thread."""
    import asyncio as _asyncio
    from .connector_probes import probe_one
    res = await _asyncio.to_thread(probe_one, name)
    return res.to_dict()


@app.get("/connectors/journey")
async def connectors_journey(only: str = "") -> dict:
    """Read-only connector lifecycle map for Setup/chat: not set up vs configured-but-broken vs
    working, whether safe read-only sync is available, and the exact next action a real user should
    take. This is the antidote to integrations showing a green tile while their real use case fails."""
    import asyncio as _asyncio
    from .connector_probes import connector_journey
    names = [n.strip() for n in only.split(",") if n.strip()] or None
    return await _asyncio.to_thread(connector_journey, only=names)


@app.get("/self/reflect")
async def self_reflect() -> dict:
    """Read-only self-audit of Mongoose's own wiring: does every capability that exists in code actually
    reach the single brain (read-only-registered, in the planner catalogue, status areas callable)?
    Reports gaps, changes nothing. The 'notice a feature should be there but isn't' check."""
    import asyncio as _asyncio
    from .services.self_reflection import reflect
    return await _asyncio.to_thread(reflect, apply_fixes=False)


@app.post("/connectors/verify/{name}")
async def connectors_verify(name: str, request: Request) -> dict:
    """Re-probe one connector on demand (the 'check it again' action). Read-only. Nonce-gated since it's
    a POST action, though it only reads."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    from .connector_probes import verify_connector
    return await _asyncio.to_thread(verify_connector, name)


@app.post("/connectors/sync/{name}")
async def connectors_sync(name: str, request: Request) -> dict:
    """Pull fresh READ-ONLY data for one connector (gmail/google_calendar/imap/microsoft). Probes first;
    won't sync a connector that's down. Never sends/deletes/moves anything. Nonce-gated + off-thread."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    from .connector_probes import sync_connector
    return await _asyncio.to_thread(sync_connector, name)


@app.post("/connectors/sync-all")
async def connectors_sync_all(request: Request) -> dict:
    """Sync every connector with a safe read-only data pull, sequentially (no provider hammering).
    Read-only toward providers. Nonce-gated + off-thread."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    from .connector_probes import sync_all_read_only
    return await _asyncio.to_thread(sync_all_read_only)


@app.post("/self/reflect-and-fix")
async def self_reflect_and_fix(request: Request) -> dict:
    """Run the self-audit AND apply the bounded, reversible auto-fixes (runtime read-only-tool
    registration only, never writes code), then, if v3 is enabled, notify the peers so Claude Chat
    gets visibility into the self-applied patch. Nonce-gated because it mutates runtime state."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.self_reflection import reflect_and_notify
    return await _asyncio.to_thread(reflect_and_notify, apply_fixes=True)


@app.post("/cockpit/readiness/refresh")
def cockpit_readiness_refresh() -> dict:
    """Force a fresh readiness check now (the 'check now' button), bypassing the cache."""
    from .services.readiness import refresh_readiness
    return refresh_readiness()


@app.get("/cockpit/readiness/voice")
def cockpit_readiness_voice() -> dict:
    """The short spoken-style readiness answer (for a voice surface or a quick glance)."""
    from .services.readiness_presenter import readiness_for_voice
    return readiness_for_voice()


@app.get("/cockpit/tuning")
def cockpit_tuning() -> dict:
    """Lane 2 state for the cockpit card: whether tuning has vested, and if so the
    recommendation. Read-only; safe to poll. The card only shows itself when vested+actionable."""
    from .model_tuning import evaluate
    # SWR-cached: tuning evaluation is safe to poll but not free; never make an interactive poll pay for it.
    return _status_cached("tuning", 60.0, lambda: evaluate(allow_auto_apply=False))


@app.post("/cockpit/tuning/{action}")
def cockpit_tuning_action(action: str, request: Request) -> dict:
    """Apply / revert / dismiss a tuning recommendation. Nonce-gated like other cockpit
    actions, so an injected page can't switch the user's models."""
    nonce = request.headers.get("x-cockpit-nonce", "")
    if not nonce or not _secrets.compare_digest(nonce, _COCKPIT_NONCE):
        raise HTTPException(status_code=403, detail="cockpit action nonce required")
    from . import model_tuning as MT
    if action == "apply":
        return MT.apply_recommendation()
    if action == "revert":
        return MT.revert()
    if action == "dismiss":
        return MT.dismiss()
    raise HTTPException(status_code=400, detail="action must be apply, revert, or dismiss")


@app.get("/cockpit/oauth-config")
def cockpit_oauth_config() -> dict:
    """OAuth configuration for the setup UI: the exact redirect URI to register, port, and whether
    that port is currently free. The UI reads this so it always shows the live value and can warn the
    user that the port is busy BEFORE they click Sign in (instead of failing afterwards)."""
    try:
        from .oauth import (google_oauth_redirect_uri, google_oauth_redirect_port,
                            _port_is_free, _describe_port_holder)
        port = google_oauth_redirect_port()
        free = _port_is_free(port)
        out = {"google_redirect_uri": google_oauth_redirect_uri(),
               "google_redirect_port": port,
               "port_free": free}
        if not free:
            # The registered port is a FIXED target. If it is busy, the fix is to free it, not to move
            # to another port (moving is what produced the endless 8766->8767->8768 climb). Keep the
            # registered URI stable and tell the user what is holding the port.
            holder = _describe_port_holder(port)
            out["warning"] = (
                f"Port {port} is busy right now, so sign-in can't start. Your registered URI "
                f"{out['google_redirect_uri']} is still correct, free the port and try again: {holder} "
                f"To use a different port permanently, set GOOGLE_OAUTH_REDIRECT_PORT in .env and "
                f"register that one URI once. Mongoose will not change the port on its own.")
        return out
    except Exception as exc:
        return {"google_redirect_uri": "http://localhost:8766/",
                "google_redirect_port": 8766,
                "_error": f"oauth config degraded: {exc}"}


def _degraded_wizard_payload(exc: Exception) -> dict:
    return {"stages": [], "core_done": False,
            "core_status": {"ready": False, "items": [
                {"ok": False, "label": "Setup schema error", "detail": str(exc)}]},
            "configured_count": 0, "_error": str(exc)}


@app.get("/cockpit/wizard")
def cockpit_wizard() -> dict:
    """The setup wizard schema (stages + integrations + current values), projected from
    the same INTEGRATIONS registry the CLI wizard uses. Secrets are never returned.
    Never returns 500: on any error returns a safe degraded schema so the setup tab loads."""
    try:
        from .wizard import wizard_schema
        return wizard_schema()
    except Exception as exc:
        import traceback as _tb
        _tb.print_exc()   # still log it server-side
        return _degraded_wizard_payload(exc)


@app.get("/cockpit/devices")
def cockpit_devices() -> dict:
    """List paired devices with what each can do for Mongoose, freshness, and next step."""
    from .devices import list_devices, device_receipts
    return {"devices": list_devices(), "receipts": device_receipts(20)}


@app.post("/device/capabilities")
async def device_capabilities(request: Request) -> dict:
    """A paired device reports which capabilities it has enabled (e.g. after the user
    granted a permission). Authenticated by the device's own token (the guard already
    verified it); the device may only update ITSELF."""
    from .devices import set_device_capabilities
    dev = _device_from_request(request)
    payload = await request.json()
    enabled = payload.get("enabled", [])
    return set_device_capabilities(dev["device_id"], enabled)


@app.get("/device/whoami")
def device_whoami_route(request: Request) -> dict:
    """Safe paired-device metadata: role, capabilities, freshness, next step.
    This is what a phone/tablet PWA calls after pairing to prove it is connected."""
    from .devices import device_whoami
    dev = _device_from_request(request)
    state = device_whoami(dev["device_id"])
    if not state:
        raise HTTPException(status_code=404, detail="device not found")
    return {"ok": True, "device": state}


@app.post("/device/profile")
async def device_profile_route(request: Request) -> dict:
    """First-run identity: the person tells Mongoose the name they want to be known by.
    Captured right after pairing, before anything else, so the brain always has a real
    human name for this device. If the device is the owner's, this also sets the owner's
    name used across every owner surface. Device-token auth."""
    from .devices import set_device_display_name, device_whoami
    dev = _device_from_request(request)
    payload = await request.json()
    name = str(payload.get("display_name") or payload.get("name") or "").strip()[:40]
    if not name:
        return {"ok": False, "error": "a name is required"}
    res = set_device_display_name(dev["device_id"], name)
    # If this is an owner-class device (the primary phone/tablet), this name is also how the
    # single owner identity is addressed everywhere (chat, messages, notifications).
    is_guest = (dev.get("profile") == "trusted_guest")
    if not is_guest:
        try:
            from .db import set_app_state
            set_app_state("owner_display_name", name)
        except Exception:
            pass
    res["is_owner_device"] = (not is_guest)
    res["device"] = device_whoami(dev["device_id"])
    return res


@app.post("/device/heartbeat")
def device_heartbeat_route(request: Request) -> dict:
    from .devices import heartbeat
    dev = _device_from_request(request)
    return heartbeat(dev["device_id"])


@app.post("/device/event")
async def device_event_route(request: Request) -> dict:
    """Allowlisted device-origin state event. This is the safe Mac-side envelope for
    future Android Companion capabilities; it is not a general API tunnel."""
    from .devices import record_device_event
    dev = _device_from_request(request)
    payload = await request.json()
    return record_device_event(dev["device_id"], payload.get("type", ""),
                               payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
                               payload.get("privacy", "medium"))




@app.get("/tutorial")
def tutorial_intro() -> dict:
    """Public first-run tutorial. It deliberately does NOT inspect private readiness
    state, because /tutorial is reachable before auth and from a newly paired phone.
    Authenticated Cockpit/device surfaces use their own routes for live capability cards.
    """
    from .services.tutorial import intro_for_phone, intro_speech
    data = intro_for_phone(include_private_state=False)
    data["speech"] = intro_speech(include_private_state=False)
    return data


@app.post("/tutorial/cool")
async def tutorial_cool() -> dict:
    """Public demo endpoint: sample only, never private real data. The authenticated
    Cockpit route may show real cross-domain links."""
    from .services.tutorial import do_something_cool
    return await do_something_cool(allow_private_data=False)


@app.get("/cockpit/tutorial")
def cockpit_tutorial_intro() -> dict:
    from .services.tutorial import intro_for_phone, intro_speech
    data = intro_for_phone(include_private_state=True)
    data["speech"] = intro_speech(include_private_state=True)
    return data


@app.post("/cockpit/tutorial/cool")
async def cockpit_tutorial_cool() -> dict:
    from .services.tutorial import do_something_cool
    return await do_something_cool(allow_private_data=True)


@app.get("/device/tutorial")
def device_tutorial_intro(request: Request) -> dict:
    dev = _device_from_request(request)
    from .services.tutorial import intro_for_phone, intro_speech
    # A guest tutorial must not reveal owner readiness (which integrations exist, whether mail/HA are
    # configured, sync state). Force the public, non-private variant for guests.
    private = (dev.get("profile") != "trusted_guest")
    data = intro_for_phone(include_private_state=private)
    data["speech"] = intro_speech(include_private_state=private)
    return data


@app.post("/device/tutorial/cool")
async def device_tutorial_cool(request: Request) -> dict:
    dev = _device_from_request(request)
    from .services.tutorial import do_something_cool
    # Never let a guest's "do something cool" touch private data or kick off a mail sync.
    allow_private = (dev.get("profile") != "trusted_guest")
    return await do_something_cool(allow_private_data=allow_private)


@app.get("/cockpit/alerts")
def cockpit_alerts() -> dict:
    """Upcoming alarms, timers and reminders for the cockpit/phone cards. Flags any that are
    native-but-unverified so the user can see what wasn't confirmed."""
    from .services.time_alerts import list_alerts
    alerts = list_alerts()
    return {
        "alerts": alerts,
        "count": len(alerts),
        "unverified": [a for a in alerts if a.get("status") == "accepted_unverified"],
    }


@app.post("/cockpit/alerts/cancel/{alert_id}")
def cockpit_alert_cancel(alert_id: str, request: Request) -> dict:
    """Cancel a single alert by id. Nonce-gated (so an injected page can't clear your alarms)."""
    nonce = request.headers.get("x-cockpit-nonce", "")
    if not nonce or not _secrets.compare_digest(nonce, _COCKPIT_NONCE):
        raise HTTPException(status_code=403, detail="cockpit action nonce required")
    from .services.time_alerts import cancel_alert
    res = cancel_alert(alert_id)
    return {"ok": bool(res), "alert": res}


@app.get("/device/readiness")
async def device_readiness_route(request: Request) -> dict:
    """Phone-shaped readiness for a paired device: status headline, top action cards, what's
    ready, and what THIS device can do. Device-token scoped; no secrets, no raw private content."""
    from .services.readiness_presenter import readiness_for_phone
    dev = _device_from_request(request)
    return readiness_for_phone(device_id=dev.get("device_id"))


@app.get("/device/remote-access/router")
async def device_router_card_route(request: Request) -> dict:
    """Mongoose's own friendly 'set this up on your router' card for a paired phone, the exact fields
    (service name, server IP, port, protocol) already filled in, plus a warm plain-language
    explanation. If no port-forward is needed (e.g. Tailscale, or local-only), it says so happily."""
    _device_from_request(request)
    from .ddns import port_forward_card
    return {"ok": True, **port_forward_card()}


@app.get("/cockpit/remote-access/router")
async def cockpit_router_card_route() -> dict:
    """FIX: the rendered card checked for rt.ddns/rt.ports/rt.headline, but port_forward_card()
    never produced any of those keys, so this card could never actually render -- a dead UI
    surface with a live-looking backend call. Now returns the REAL setup_state remote_away
    diagnosis (the same one the pairing page and /device/remote/repair already use), plus the
    router-fields data for when a manual port-forward genuinely needs setting up."""
    from .ddns import port_forward_card
    from .setup_state import setup_state
    card = port_forward_card()
    remote = setup_state(only=["remote_away"])["surfaces"]["remote_away"]
    return {"ok": True, **card, "remote_away": remote}


@app.get("/device/surprise")
async def device_surprise_route(request: Request) -> dict:
    """A surprise for the CALLING device. For a guest, this routes to the RESTRICTED guest surpriser
    (delights/entertainment + any message the owner left them), it never touches the owner's real-world data. For
    the owner's own devices it uses the full surpriser."""
    dev = _device_from_request(request)
    is_guest = (dev.get("profile") == "trusted_guest")
    if is_guest:
        from .services.guest_surpriser import guest_surprise
        scope = f"guest:{dev['device_id']}"
        prefer = request.query_params.get("prefer", "auto")
        return guest_surprise(scope, prefer=prefer)
    # owner device, full surpriser (short timeout so a busy model can't hang the call)
    import asyncio as _aio
    from .services.surpriser import pick_surprise
    try:
        return await _aio.wait_for(pick_surprise(force=True, source="on_demand"), timeout=25.0)
    except _aio.TimeoutError:
        return {"surfaced": False, "reason": "suggestion timed out, try again in a moment"}


@app.get("/device/guest/can-do")
async def device_guest_can_do_route(request: Request) -> dict:
    """What a guest can do, the light, low-stakes help available to them (coach / focus / plan /
    research / reminders / fun). Mongoose surfaces this so guests know what's on offer."""
    dev = _device_from_request(request)
    if dev.get("profile") != "trusted_guest":
        # Owners don't need the guest blurb; keep it guest-only to avoid confusion.
        return {"ok": True, "note": "guest capabilities apply to guest devices"}
    from .services.guest_surpriser import guest_can_do
    return {"ok": True, **guest_can_do()}


@app.post("/device/guest/task")
async def device_guest_task_route(request: Request) -> dict:
    """Run a light guest capability. Body: {action, text|task|question|tasks}. Guest-only and
    scope-isolated, a guest's reflections/plans/reminders live in their own namespace and can never
    touch the owner's tools, data, or scheduler. action in:
    reflect | focus | plan | research | remind | note | list | complete."""
    dev = _device_from_request(request)
    if dev.get("profile") != "trusted_guest":
        raise HTTPException(status_code=403, detail="guest tasks are for guest devices only")
    scope = f"guest:{dev['device_id']}"
    from .services import guest_tasks as GT
    body = await request.json()
    action = (body.get("action") or "").strip().lower()
    try:
        if action == "reflect":
            return await GT.reflect(scope, body.get("text", ""))
        if action == "focus":
            return await GT.focus_step(scope, body.get("task", body.get("text", "")))
        if action == "research":
            return await GT.quick_research(scope, body.get("question", body.get("text", "")))
        if action == "plan":
            return GT.day_plan(scope, body.get("tasks", []))
        if action in ("remind", "note", "todo"):
            kind = "reminder" if action == "remind" else action
            return GT.add_item(scope, kind=kind, text=body.get("text", ""), due_at=body.get("due_at"))
        if action == "list":
            return {"ok": True, "items": GT.list_items(scope, include_done=bool(body.get("include_done")))}
        if action == "complete":
            return GT.complete_item(scope, int(body.get("id", 0)))
        return {"ok": False, "error": "unknown action"}
    except ValueError as exc:
        # the _require_guest guard (static message) or a bad int. Return the guard's own message when it
        # is one of our controlled strings; otherwise a generic message, never echo arbitrary exc text.
        msg = str(exc)
        safe = msg if msg.startswith("guest ") or "guest device" in msg else "invalid request"
        return {"ok": False, "error": safe}


@app.post("/device/upload")
async def device_upload_route(request: Request) -> dict:
    """Upload a file from a paired phone/tablet, build materials, a plugin, code, a document. The bytes
    are saved as an ASSET with a stable asset id, the single brain records it, and it's classified:
    a correctly-structured Mongoose build zip becomes available for self-update; other code/archives
    become available for RTM enrichment. Returns the asset id + classification.

    Auth: a valid paired device token (x-mongoose-device-token). Filename comes from the
    X-Filename header or ?filename= (a phone PWA sets it); body is the raw file bytes (any content type).
    Content-addressed: re-uploading identical bytes returns the same asset id (idempotent)."""
    dev = _device_from_request(request)
    from .services import assets as _assets
    filename = (request.headers.get("x-filename", "")
                or request.query_params.get("filename", "")
                or "upload.bin")
    mime = request.headers.get("content-type", "") or None
    note = request.headers.get("x-note", "") or request.query_params.get("note", "") or None
    raw = await request.body()
    if not raw:
        return {"ok": False, "error": "empty upload; POST the raw file bytes with an X-Filename header"}
    # Guard against absurd sizes at the surface (the updater enforces its own checks downstream).
    if len(raw) > 256 * 1024 * 1024:
        return {"ok": False, "error": "upload exceeds 256MB limit"}
    # Body-data CSVs (Alpha Progression sessions/sets/body-comp, MyFitnessPal) are imported straight
    # into the body-state layer from the phone, the same as the cockpit Body data card, and silence the
    # data nudge. Only the owner's own data: a guest device never reaches this route (default-deny).
    if (filename or "").lower().endswith(".csv"):
        try:
            text = raw.decode("utf-8", errors="replace")
            from .services.body_data_nudge import _sniff_kind, ingest_csv
            if _sniff_kind(filename, text) is not None:
                res = ingest_csv(filename, text)
                res["surface"] = "phone"
                return res
        except Exception:
            pass  # fall through to generic asset storage if it isn't body data after all
    rec = _assets.store_asset(raw, filename, uploaded_by=dev.get("device_id"),
                              source_surface=dev.get("profile") or "phone", mime_type=mime, note=note)
    if not rec.get("ok"):
        return rec
    # A friendly next-step hint for the phone UI.
    if rec.get("available_for_self_update"):
        rec["next"] = ("This is a valid Mongoose build. It can be applied with "
                       "`mongoose update --stage-asset " + rec["asset_id"] + "` or from the Build tab.")
    elif rec.get("available_for_rtm"):
        rec["next"] = "Saved and available for RTM enrichment and reference."
    return rec


@app.post("/device/chat/attach")
async def device_chat_attach_route(request: Request) -> dict:
    """A file attached to a chat message from a paired phone (<= 30MB). Body-data CSVs are imported;
    everything else is stored as a referenceable asset. Owner devices only (a guest token is rejected
    by the global gate before reaching here, since /device/chat/attach is not in the guest allowlist).

    Raw body with X-Filename (the phone PWA sets it). Returns the lane + a summary for the chat UI."""
    dev = _device_from_request(request)
    if (dev.get("profile") or "") != "primary_phone":
        return JSONResponse(status_code=403, content={"ok": False, "error": "owner devices only"})
    from .services.uploads import handle_chat_attachment
    filename = (request.headers.get("x-filename", "")
                or request.query_params.get("filename", "") or "upload.bin")
    mime = request.headers.get("content-type", "") or None
    raw = await request.body()
    if not raw:
        return {"ok": False, "error": "empty upload; POST the raw file bytes with an X-Filename header"}
    return handle_chat_attachment(filename, raw, uploaded_by=dev.get("device_id"),
                                  surface="phone-chat", mime_type=mime)


@app.get("/device/assets")
async def device_assets_route(request: Request) -> dict:
    """List assets the requesting device can see. Owner devices get the asset catalogue (minus internal
    stored paths, which are server internals). Guest devices get an empty list: they have no business
    browsing the asset catalogue, and exposing filenames + stored paths to a guest tablet would be a
    privacy boundary violation."""
    dev = _device_from_request(request)
    if dev.get("profile") == "trusted_guest":
        # Guests get no asset listing: they have guest-scoped conversation and tasks, not file access.
        return {"ok": True, "assets": [], "note": "asset listing is not available to guest devices"}
    from .services import assets as _assets
    kind = request.query_params.get("kind") or None
    records = _assets.list_assets(kind=kind, limit=200)
    # Strip stored_path from device-facing responses: it's a local filesystem path and has no value
    # to a device client while being exactly the kind of internal detail that shouldn't leave this Mac.
    for rec in records:
        rec.pop("stored_path", None)
    return {"ok": True, "assets": records}


@app.get("/device/assets/{asset_id}")
async def device_asset_get_route(asset_id: str, request: Request) -> dict:
    """Metadata for one asset by id."""
    _device_from_request(request)
    from .services import assets as _assets
    rec = _assets.get_asset(asset_id)
    if not rec:
        raise HTTPException(status_code=404, detail="asset not found")
    return {"ok": True, **rec}


# Short-TTL cache for the phone Status summary (keyed by device_id): caps the heavy recompute to once
# per TTL per device however fast the phones poll, so polling load cannot saturate the event loop.
_SUMMARY_CACHE: dict[str, tuple[float, dict]] = {}
_SUMMARY_TTL = 12.0
# Single-flight guard: device_ids with a background refresh already in flight. Without this, every
# stale poll (both phones, every few seconds) spawned ANOTHER refresh while the previous was still
# running ~2s of aggregation , they piled up and pegged the CPU. One refresh at a time per device.
_SUMMARY_REFRESHING: set[str] = set()


def _summary_pending_drafts() -> list:
    """Pending WhatsApp drafts count source, isolated so it can run off the event loop. Best-effort."""
    try:
        from .services.whatsapp import list_drafts
        return list_drafts("pending") or []
    except Exception:
        return []


async def _build_owner_summary(state: dict, *, budget: float = 2.0) -> dict:
    """Compute the owner Status-card payload with EVERY heavy section hard-bounded to `budget` seconds
    and run off the event loop. A section that misses the budget comes back empty and fills on a later
    refresh, so the card returns within ~budget and the UI never hangs."""
    import asyncio as _asyncio
    from .services.journal_layer import what_changed

    async def _guarded(coro, default):
        try:
            return await _asyncio.wait_for(coro, budget)
        except Exception:
            return default

    async def _dop():
        from .services.daily_picture import daily_operating_picture
        return await daily_operating_picture()

    async def _suggest():
        from .services.surpriser import candidate_suggestions, PROACTIVE_NOISE_KINDS
        cands = await candidate_suggestions()
        _CONTENT = {"music_interview", "music_rec"} | PROACTIVE_NOISE_KINDS
        return [c for c in cands if c.get("kind") not in _CONTENT][:3]

    _wc_default = {"changed": False, "count": 0, "summary": "", "changes": [], "detail": {}}
    dop, suggestions, wc, pending = await _asyncio.gather(
        _guarded(_dop(), {}),
        _guarded(_suggest(), []),
        _guarded(_asyncio.to_thread(what_changed, acknowledge=False), _wc_default),
        _guarded(_asyncio.to_thread(lambda: len(_summary_pending_drafts())), 0),
    )
    return {
        "ok": True, "health": {"version": __version__}, "device": state, "what_changed": wc,
        "daily": {"next_smallest_step": dop.get("next_smallest_step"), "counts": dop.get("counts", {}),
                  "what_needs_you": dop.get("what_needs_you", [])[:5]},
        "suggestions": suggestions, "approvals": {"pending": pending},
    }


async def _refresh_owner_summary(dk: str, state: dict) -> None:
    """Background refresh of a device's Status cache with a generous budget (off the UI critical path).
    Always clears the single-flight flag so a later poll can refresh again."""
    try:
        import time as _time
        payload = await _build_owner_summary(state, budget=6.0)
        _SUMMARY_CACHE[dk] = (_time.monotonic(), payload)
    except Exception:
        pass
    finally:
        _SUMMARY_REFRESHING.discard(dk)


@app.get("/device/summary")
async def device_summary_route(request: Request) -> dict:
    """Compact mobile/PWA launchpad data for a paired phone/tablet. It deliberately
    returns summaries and counts, not raw secrets. The Mac remains the brain."""
    from .devices import device_whoami
    dev = _device_from_request(request)
    state = device_whoami(dev["device_id"]) or dev
    # Guests get their OWN compact summary: no owner what-changed, no daily operating picture, no
    # owner suggestions, no owner approval queue. Those are the owner's private state and have no
    # place on a guest tablet. (The gate already blocks owner-only routes; this scopes the one
    # owner-data route a guest is allowed to call.)
    if dev.get("profile") == "trusted_guest":
        return {
            "ok": True,
            "guest": True,
            "health": {"version": __version__},
            "device": state,
            "what_changed": {"changed": False, "count": 0,
                             "summary": "Your guest space is ready.", "changes": [], "detail": {}},
            "daily": {"next_smallest_step": None, "counts": {}, "what_needs_you": []},
            "suggestions": [],
            "approvals": {"pending": 0},
        }
    # Stale-while-revalidate so the Status card NEVER makes the UI wait. The heaviest section
    # (daily_operating_picture) is ~1.8s alone, so blocking a poll on a fresh compute felt slow and,
    # under two phones + tunnel retries, saturated the loop. Instead: return the cached card INSTANTLY
    # (even if a little stale) and refresh in the background; only the very first load for a device
    # computes inline, and that is hard-bounded to 2s so it still can't hang.
    import time as _time
    import asyncio as _asyncio
    _dk = str(state.get("device_id") or dev.get("device_id") or "")
    now = _time.monotonic()
    _cached = _SUMMARY_CACHE.get(_dk)
    if _cached:
        ts, payload = _cached
        if (now - ts) >= _SUMMARY_TTL and _dk not in _SUMMARY_REFRESHING:
            _SUMMARY_REFRESHING.add(_dk)
            _SUMMARY_CACHE[_dk] = (now, payload)  # keep serving this + stop RE-triggering until the refresh lands
            _asyncio.create_task(_refresh_owner_summary(_dk, state))  # exactly one refresh at a time
        return payload
    # Cold first load: HARD 2s ceiling. If the build (already per-section bounded) still overruns,
    # ship an empty card instantly and let the background refresh fill it on the next poll. The UI
    # never waits past 2s, ever.
    try:
        payload = await _asyncio.wait_for(_build_owner_summary(state, budget=1.8), 2.0)
    except Exception:
        payload = {"ok": True, "health": {"version": __version__}, "device": state,
                   "what_changed": {"changed": False, "count": 0, "summary": "", "changes": [], "detail": {}},
                   "daily": {"next_smallest_step": None, "counts": {}, "what_needs_you": []},
                   "suggestions": [], "approvals": {"pending": 0}}
        _asyncio.create_task(_refresh_owner_summary(_dk, state))
    _SUMMARY_CACHE[_dk] = (now, payload)
    return payload


@app.get("/device/delight")
def device_delight_route(request: Request, category: str = "", exclude: str = "") -> dict:
    """A fun fact for a paired device. Draws from the large, self-deduping delight cache (hundreds of
    items, each served at most once via the used flag), so the card/voice line stops going stale on the
    same fact. Owner devices read the general well; guests read the neutral guest well. Falls back to
    the small curated pool only if the cache is empty. Device-token auth."""
    dev = _device_from_request(request)  # any paired device may receive delights
    audience = "general" if str(dev.get("profile") or "") == "primary_phone" else "guest"
    try:
        from .services import delight_cache as _dc
        from .services.delight import _recent_sigs, _remember
        drawn = _dc.draw(audience=audience, kind="fact", recent_sigs=_recent_sigs())
        if drawn and drawn.get("text"):
            try:
                _remember(drawn["sig"])  # share the recent window with the chat/voice delight surface
            except Exception:
                pass
            return {"text": drawn["text"], "style": category or "self",
                    "source": drawn.get("source"), "caveat": drawn.get("caveat")}
    except Exception:
        pass
    # Cache empty (e.g. before first top-up or fully offline): curated guest-safe pool, honouring the
    # single-item exclude the card sends so at least the immediate repeat is avoided.
    from .services.guest_delights import random_fact
    return random_fact(category=category or None, exclude=exclude or None)


@app.get("/device/entertain")
def device_entertain_route(request: Request, kind: str = "") -> dict:
    """The 'what can I do with you' fun menu + interactive bits for a guest (fact / would_you_rather /
    riddle / compliment / mood / menu). Device-token auth, guest-safe."""
    _device_from_request(request)
    from .services.guest_delights import entertain
    return entertain(kind)


@app.post("/device/share")
async def device_share_route(request: Request) -> dict:
    """PWA precursor to Android Share Sheet: save bounded text/link into Mongoose's
    local bus as a note from this paired device."""
    from .devices import share_to_mongoose
    dev = _device_from_request(request)
    payload = await request.json()
    return share_to_mongoose(dev["device_id"], title=payload.get("title", ""),
                             note=payload.get("note", ""), url=payload.get("url", ""),
                             project=payload.get("project", ""))


import re as _re_mb

_MSG_SEND_PATTERNS = [
    # "tell/message/text <name> <body>": but NOT when <name> is a pronoun (me/us/you/him/her/them),
    # an article (a/an/the), or "everyone" (handled separately). This stops "tell a joke",
    # "tell me a joke", "tell us a story" from being treated as messages to a guest. The recipient
    # must look like a name: a word that isn't a stop-word.
    _re_mb.compile(
        r"^\s*(?:tell|message|msg|text)\s+"
        r"(?!me\b|us\b|you\b|him\b|her\b|them\b|a\b|an\b|the\b|everyone\b|all\b|us\b)"
        r"(?P<who>[a-z][a-z0-9._-]{0,40}?)\s+(?:know\s+)?(?:that\s+)?(?P<body>.+)$", _re_mb.I),
    _re_mb.compile(
        r"^\s*let\s+"
        r"(?!me\b|us\b|you\b|him\b|her\b|them\b|a\b|an\b|the\b)"
        r"(?P<who>[a-z][a-z0-9._-]{0,40}?)\s+know\s+(?:that\s+)?(?P<body>.+)$", _re_mb.I),
    _re_mb.compile(r"^\s*(?:send|leave)\s+(?:a\s+)?(?:message|note)\s+(?:to|for)\s+(?P<who>[a-z][a-z0-9 ._-]{0,40}?)\s*[:,]?\s+(?P<body>.+)$", _re_mb.I),
]
_MSG_READ_PATTERNS = [
    _re_mb.compile(r"^\s*(?:read|show|check|any)\s+(?:my\s+)?(?:new\s+)?messages?\b", _re_mb.I),
    _re_mb.compile(r"^\s*messages?\s*\??\s*$", _re_mb.I),
    # natural phrasings: "did Sam say anything", "anything from Sam", "what did Sam send",
    # "has Sam messaged", "any word from Sam". Negative lookahead excludes you/I/we/it so
    # "did you say something" / "what did I say" stay general questions, not message reads.
    _re_mb.compile(r"\b(?:did|has|have)\s+(?!you\b|i\b|we\b|it\b|the\b|anything\b)\w+\s+(?:say|said|send|sent|messag|leave|left|writ)", _re_mb.I),
    _re_mb.compile(r"\banything\s+(?:from|by)\s+(?!you\b|me\b|us\b)\w+", _re_mb.I),
    _re_mb.compile(r"\bany\s+(?:word|news|messages?|notes?)\s+(?:from|by)\s+(?!you\b|me\b|us\b)\w+", _re_mb.I),
    _re_mb.compile(r"\bwhat\s+did\s+(?!you\b|i\b|we\b)\w+\s+(?:say|send|leave|write)", _re_mb.I),
    # "any new notifications", "check my notifications", "what are my notifications": the owner
    # often calls a waiting message a "notification". Route these to the mailbox read too.
    _re_mb.compile(r"\b(?:any|new|unread|check|my)\s+(?:new\s+)?notifications?\b", _re_mb.I),
    _re_mb.compile(r"\bwhat(?:'?s| are|\s+are)?\s+(?:my\s+)?(?:new\s+)?notifications?\b", _re_mb.I),
    # "a message from Sam", "there should be a message from Sam", "messages from Sam"
    _re_mb.compile(r"\bmessages?\s+from\s+(?!me\b|you\b|us\b)\w+", _re_mb.I),
    _re_mb.compile(r"^\s*(?:any|anything)\s+new\s*\??\s*$", _re_mb.I),
]


_DELIGHT_PATTERNS = [
    _re_mb.compile(r"\bwhat\s+can\s+(?:i|we|you)\s+(?:do|help)\b", _re_mb.I),
    _re_mb.compile(r"\bhow\s+can\s+you\s+help\b", _re_mb.I),
    _re_mb.compile(r"\bsurprise\s+me\b", _re_mb.I),
    _re_mb.compile(r"\b(?:fun\s+fact|tell\s+me\s+(?:a\s+)?(?:fun\s+)?fact|something\s+interesting|something\s+fun)\b", _re_mb.I),
    _re_mb.compile(r"\bcheer\s+me\s+up\b", _re_mb.I),
    _re_mb.compile(r"\b(?:would\s+you\s+rather|give\s+me\s+a\s+riddle|say\s+something\s+nice)\b", _re_mb.I),
    _re_mb.compile(r"\b(?:i'?m\s+bored|entertain\s+me)\b", _re_mb.I),
]


_CAPABILITIES_RE = _re_mb.compile(
    r"\bwhat can (i|we|you) (do|use|get up to)\b|\bwhat can we do together\b|"
    r"\bwhat are you (good|useful) (for|at)\b|\bwhat can you help (me )?with\b",
    _re_mb.IGNORECASE)


# "benchmark", OR a model-family name (gemma/qwen/gpt-oss/...), OR a model size like 12b/26b/30b. The
# family/size tokens route follow-ups like "how is gemma4 12b doing?" or "compare 12b vs 26b" to the
# GROUNDED benchmark answer (real scoreboard) instead of letting them fall to bare chat, which fabricated
# per-model scores that don't exist.
_BENCHMARK_RE = _re_mb.compile(
    r"\bbenchmark|\b(gemma|qwen|gpt[-\s]?oss|llama|mistral|phi|deepseek)\b|\b\d{1,3}b\b",
    _re_mb.IGNORECASE)


def _semantic_intent_is(message: str, intent: str) -> bool:
    """Embeddings fallback for the app-level chat hooks (benchmark, capabilities): True if the UNIFIED
    intent resolver's best intent for this message is `intent`. Routed through intent_resolver (not the
    deprecated semantic_router) so benchmark/capabilities on the phone path use the same one classifier
    as the rest of the brain. Cheap False when embeddings are unavailable. Never raises."""
    try:
        from .services.intent_resolver import resolve
        return resolve(message)[0] == intent
    except Exception:
        return False


async def _maybe_benchmark_answer(message: str, *, is_guest: bool) -> str | None:
    """Owner asking about benchmarks. GROUNDED, not canned (the owner's principle: always think, never recite a
    memorised line): we fetch the REAL benchmark state deterministically, then let the model phrase a
    reply in context (so a follow-up like 'neither Status nor Cockpit has it' gets an actual answer, not
    the same block again). The deterministic facts double as the fallback if the model is unavailable."""
    if is_guest:
        return None
    if not _BENCHMARK_RE.search(message or ""):
        # Semantic-only match: require a substantive query (5+ words). A short reaction like "that's
        # stale" (a comment on the delight, NOT a benchmark question) must never mis-fire this handler ,
        # that is exactly what routed "that's stale" to a benchmark reply that then looped.
        if len((message or "").split()) < 5 or not _semantic_intent_is(message, "benchmark"):
            return None
    try:
        from .services.benchmark_surpriser import latest_benchmark_visibility
        v = latest_benchmark_visibility()
    except Exception:
        return None
    on = "on" if v.get("enabled") else "off"
    window = v.get("window") or "05:00-08:00 local"
    summ = v.get("summary") or {}
    models = summ.get("models") or []
    n = summ.get("result_count") or len(models)

    def _local(iso: str) -> str:  # local time, never raw UTC (the owner's rule)
        try:
            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo
            dt = datetime.fromisoformat((iso or "").replace("Z", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            tz = getattr(get_settings(), "timezone", "") or "Europe/London"
            return dt.astimezone(ZoneInfo(tz)).strftime("%-d %b %Y %H:%M %Z")
        except Exception:
            return (iso or "")[:16].replace("T", " ")

    where = "The results live under System (searching 'benchmarks' jumps straight there), not Status or Cockpit."
    # The REAL per-model scoreboard, so any 'how did model X do' / 'compare X and Y' answer is grounded in
    # actual measured numbers. Without this the model invented per-model rankings that do not exist.
    scoreboard = ""
    try:
        from .benchmark import benchmark_scoreboard_text
        scoreboard = benchmark_scoreboard_text()
    except Exception:
        scoreboard = ""
    _what = ("The benchmark measures OPERATIONAL fitness ONLY: startup (cold-load) and response (warm-chat, "
             "task-latency) speed, tool-call validity, and memory extraction. It does NOT measure reasoning, "
             "logic, intelligence, or answer quality, and it says nothing about a model's RAM footprint.")
    if not v.get("last_run"):
        facts = f"No benchmarks have completed yet. Overnight benchmarking is {on} (window {window})."
    elif not n and not scoreboard:
        facts = (f"The last completed benchmark run was {_local(v.get('last_run'))}. Recent overnight runs "
                 f"tested 0 models because of a bug in the candidate picker (now fixed), NOT because there "
                 f"was nothing to test: 9 models are installed. Overnight benchmarking is {on} (window "
                 f"{window}). {where}")
    else:
        facts = (f"Benchmarks last ran {_local(v.get('last_run'))}. Overnight benchmarking is {on} "
                 f"(window {window}). {where}\n{_what}\n"
                 + (f"Real per-model results from that run (these are the ONLY figures that exist, do not add "
                    f"to them):\n{scoreboard}" if scoreboard
                    else f"Models tested: {', '.join(models[:8])}. The per-model detail is under System."))

    from .llm import ask_persona
    try:
        reply = await ask_persona(
            f"The owner said: {message!r}\n\nReply briefly in your own voice, actually addressing what they "
            f"asked. Use ONLY the facts below. NEVER invent or embellish: do not state any score, ranking, "
            f"speed, comparison, or quality that is not explicitly present in the facts. The benchmark does "
            f"NOT measure reasoning/logic/intelligence, so never claim a model is better or worse at those, "
            f"and it does not measure RAM, so do not guess whether a model 'fits'. If they ask for something "
            f"the facts do not contain, say plainly you do not have that specific detail and point to System "
            f"(or, for RAM/memory, offer that they can ask you to check the live system memory). You CANNOT "
            f"look anything up or re-run right now, so do NOT say you will:\n{facts}", role="chat")
        return (reply or "").strip() or facts
    except Exception:
        return facts


async def _maybe_capabilities_answer(message: str, *, is_guest: bool) -> str | None:
    """The OWNER asking "what can I do with you?". GROUNDED, not a recited showcase (the owner's principle:
    always think, never quote a memorised block): the real capability catalog is the DATA, the model
    phrases a warm, in-context reply drawing on it, and the catalog is the fallback if the model is
    unavailable. Owner only: guests have their own delight path."""
    if is_guest:
        return None
    if not _CAPABILITIES_RE.search(message or ""):
        # Semantic-only match must be a substantive question, never a short reaction (see benchmark note).
        if len((message or "").split()) < 5 or not _semantic_intent_is(message, "capabilities"):
            return None
    # The single capability catalog (services/capability_catalog) is the same source that feeds routing;
    # it appends a gentle "once you connect X" only where a connector is genuinely missing, so grounding
    # on it never promises what cannot work yet.
    from .services.capability_catalog import owner_showcase
    showcase = owner_showcase()
    from .llm import ask_persona
    try:
        reply = await ask_persona(
            f"The owner asked: {message!r}\n\nAnswer warmly in your own voice and in context. Draw ONLY on "
            f"these real capabilities, do not invent any and do not promise anything not listed; you may "
            f"summarise and lead with what fits what they asked rather than listing everything:\n{showcase}",
            role="chat")
        return (reply or "").strip() or showcase
    except Exception:
        return showcase


def _maybe_guest_delight(message: str) -> str | None:
    """If a guest asks 'what can I do', 'surprise me', 'fun fact', etc., return a warm, curated reply
    (guest-safe, no model, no owner data). Returns None to fall through to normal chat otherwise."""
    msg = (message or "").strip().lower()
    if not any(p.search(msg) for p in _DELIGHT_PATTERNS):
        return None
    from .services.guest_delights import entertain, random_fact
    # Route to the right bit.
    if _re_mb.search(r"would\s+you\s+rather", msg):
        return "🤔 " + entertain("would_you_rather")["text"]
    if _re_mb.search(r"riddle", msg):
        r = entertain("riddle")
        return f"🧩 {r['question']}\n\n(Tap me again for the answer, or guess!)"
    if _re_mb.search(r"say\s+something\s+nice", msg):
        return "✨ " + entertain("compliment")["text"]
    if _re_mb.search(r"cheer\s+me\s+up|i'?m\s+bored|entertain\s+me", msg):
        return "🦦 " + entertain("mood")["text"]
    if _re_mb.search(r"what\s+can\s+(?:i|we|you)\s+(?:do|help)|how\s+can\s+you\s+help", msg):
        menu = entertain("menu")
        opts = "\n".join(f"  {o['emoji']} {o['label']}" for o in menu["options"])
        topics = ", ".join(menu["topics"])
        # Also surface the light, scope-isolated capabilities (coach / focus / plan / research /
        # reminders) so guests know Mongoose can genuinely help, not just entertain. These never touch
        # the owner's data, tools, or scheduler.
        help_lines = ""
        try:
            from .services.guest_surpriser import guest_can_do
            cap = guest_can_do()
            rows = cap.get("can_do", [])
            help_lines = "\n".join(
                f"  • {r['label']}: {r['blurb']}" for r in rows
                if isinstance(r, dict) and r.get("id") != "fun")
        except Exception:
            help_lines = ""
        out = "Quite a lot, actually. I can be a low-key helper while you're here:\n"
        if help_lines:
            out += help_lines + "\n\n"
        out += "And just for fun you can say:\n" + opts
        out += f"\n\nOr 'tell me a fun fact about…' any of: {topics}.\n"
        try:
            from .services import mailbox as _mb
            _owner = str(_mb.name_for(_mb.OWNER) or "").strip() or "the owner"
        except Exception:
            _owner = "the owner"
        out += (f"Anything you jot with me stays in your own space, I can't see or change {_owner}'s "
                f"emails, calendar, files, home, or schedule. I can pass a message to {_owner} for you, though.")
        return out
    # default: a fun fact
    f = random_fact()
    emoji = {"tech": "💡", "nature": "🦫", "science": "⚗️", "cosmos": "🌌", "motor": "🏎️",
             "culture": "🎭", "retro": "🎵", "joy": "🌍", "ai": "🤖",
             "self": "🦡", "host": "🙂"}.get(f["style"], "💡")
    return f"{emoji} {f['text']}"


def _maybe_handle_message_send(message: str, *, is_guest: bool, dev: dict) -> str | None:
    """Deterministically handle 'tell Alex X' / 'message Jo Y' / 'read my messages'.
    Returns a reply string if handled, or None to fall through to the LLM."""
    from .services import mailbox as mb
    participant = f"guest:{dev['device_id']}" if is_guest else mb.OWNER

    # Read intent
    for pat in _MSG_READ_PATTERNS:
        if pat.search(message):
            unread = mb.unread_for(participant)
            if not unread:
                return "You have no new messages."
            mb.mark_read(participant=participant)
            lines = []
            for m in unread:
                frm = mb._display_name(m["sender"])
                lines.append(f"• {frm}: {m['body']}")
            return "Your messages:\n" + "\n".join(lines)

    # Send intent
    for pat in _MSG_SEND_PATTERNS:
        m = pat.match(message)
        if not m:
            continue
        who = m.group("who").strip().rstrip(":,").strip()
        body = m.group("body").strip()
        if not body:
            return None
        if is_guest:
            # A guest can only message the owner; accept the owner's own name, "the owner" or "you".
            owner_aliases = {"the owner", "owner", "you", "him", "her", "them"}
            from . import profile as _profile
            owner_aliases.add(_profile.owner_name().strip().lower())
            try:
                owner_aliases.add(str(mb.name_for(mb.OWNER) or "").strip().lower())
            except Exception:
                pass
            if who.lower() in owner_aliases:
                limited = _guest_mailbox_rate_limited(str(dev.get("device_id") or ""))
                if limited:
                    return limited
                r = mb.send_message(sender=participant, recipient=mb.OWNER, body=body)
                if not r.get("ok"):
                    return f"Couldn't send: {r.get('error')}"
                owner_name = mb.name_for(mb.OWNER)
                return (f"Saved for {owner_name}. "
                        + _profile.lexicon("guest.owner_will_see",
                                           "They'll see it the next time they open Mongoose."))
            # Guests can't reach anyone else: but be friendly about it.
            return f"I can only pass messages to {_profile.owner_name()} from here."
        else:
            # Owner addressing a guest (by name) or everyone.
            recipient = mb.resolve_recipient_phrase(who)
            if not recipient or recipient == mb.OWNER:
                return f"I couldn't find a guest called \"{who}\". Add them in the cockpit Devices tab first."
            r = mb.send_message(sender=mb.OWNER, recipient=recipient, body=body)
            if not r.get("ok"):
                return f"Couldn't send: {r.get('error')}"
            if recipient == "guest:*":
                return "Saved for everyone, they'll each see it next time they open Mongoose."
            target = mb._display_name(recipient)
            if mb.recipient_is_active(recipient):
                return f"Sent to {target}."
            return f"Saved for {target}, they'll see it the next time they open Mongoose."
    return None


# Owner device profiles (a trusted_guest is deliberately excluded so a guest never receives owner pushes).
_OWNER_DEVICE_PROFILES = ("primary_phone", "home_tablet")

# Guest bug phrasings handled deterministically (before the model) so a guest can always report a
# problem, even offline-ish, without knowing any commands.
import re as _re_bug
_BUG_REPORT_RE = _re_bug.compile(
    r"^\s*(?:report(?:\s+a)?\s+bug|raise(?:\s+a)?\s+bug|log(?:\s+a)?\s+bug|there'?s?\s+a\s+bug|"
    r"found\s+a\s+bug|something(?:'?s| is)\s+(?:broken|not working|wrong)|.*\bisn'?t\s+working\b|"
    r"\bnot\s+working\b|\bbroken\b)\s*[:,-]?\s*(?P<detail>.*)$",
    _re_bug.IGNORECASE)


async def _notify_owner_of_bug(bug_id: str, summary: str, reporter_label: str) -> None:
    """Best-effort push to the owner's own devices that a bug was raised. Never fatal: the bug is always
    visible in the Approvals tab regardless of whether the push lands."""
    try:
        from .services.notifications import send_to_phone
        await send_to_phone(
            f"🐞 Bug raised by {reporter_label}",
            f"{summary} ({bug_id}) - see Approvals",
            tags="beetle", target_device_profiles=_OWNER_DEVICE_PROFILES,
        )
    except Exception:
        pass


def _maybe_handle_bug_report(message: str, *, is_guest: bool, dev: dict) -> dict | None:
    """Deterministically turn 'report a bug: X' / 'raise a bug' into a logged bug the owner sees in
    Approvals. Works for BOTH the owner and guests (it is the designed 'raise bug' feature, so it
    must fire wherever it is typed, not just on a guest device). Returns {reply, bug_id, summary,
    reporter_label} so the caller can notify the owner, or None if the message is not a bug report."""
    m = _BUG_REPORT_RE.match(message or "")
    if not m:
        return None
    detail = (m.group("detail") or "").strip() or (message or "").strip()
    if len(detail) < 3:
        return {"reply": ("Happy to log that - what's not working? Say for example "
                          "\"report a bug: the guest wifi keeps dropping\"."),
                "bug_id": "", "summary": "", "reporter_label": ""}
    from .services.bug_workflow import log_bug
    from .services import mailbox as mb
    if is_guest:
        reporter, actor = f"guest:{dev['device_id']}", f"guest:{dev['device_id']}"
    else:
        reporter, actor = mb.OWNER, "owner"
    res = log_bug(detail, actor=actor)
    bug_id = res.get("bug_id", "")
    label = mb._display_name(reporter)
    return {"reply": (f"Thanks - I've logged that as {bug_id}: \"{detail}\". "
                      f"It's in Approvals to follow up. Anything else?"),
            "bug_id": bug_id, "summary": res.get("summary", detail), "reporter_label": label}


@app.get("/device/messages")
async def device_messages(request: Request) -> dict:
    """A guest (or owner device) fetches their message thread. Device-token auth."""
    dev = _device_from_request(request)
    from .services import mailbox as mb
    is_guest = (dev.get("profile") == "trusted_guest")
    participant = f"guest:{dev['device_id']}" if is_guest else mb.OWNER
    thread = mb.list_thread(participant=participant, limit=80)
    return {"ok": True, "messages": thread, "unread": mb.unread_count(participant)}


@app.post("/device/messages/send")
async def device_messages_send(request: Request) -> dict:
    """Send a message from this device. Guests can only address the owner."""
    dev = _device_from_request(request)
    from .services import mailbox as mb
    payload = await request.json()
    body = str(payload.get("body") or "").strip()
    if not body:
        return {"ok": False, "error": "empty message"}
    is_guest = (dev.get("profile") == "trusted_guest")
    if is_guest:
        return mb.send_message(sender=f"guest:{dev['device_id']}", recipient=mb.OWNER, body=body)
    # owner device: recipient required
    to = str(payload.get("to") or "").strip()
    recipient = mb.resolve_recipient_phrase(to) or to
    return mb.send_message(sender=mb.OWNER, recipient=recipient, body=body)


@app.post("/device/messages/read")
async def device_messages_read(request: Request) -> dict:
    """Mark this device's inbound messages read."""
    dev = _device_from_request(request)
    from .services import mailbox as mb
    is_guest = (dev.get("profile") == "trusted_guest")
    participant = f"guest:{dev['device_id']}" if is_guest else mb.OWNER
    return mb.mark_read(participant=participant)


@app.get("/device/notifications")
async def device_notifications(request: Request) -> dict:
    """Recent notifications for an OWNER device, so the phone's 'what changed' can actually show
    what 'Notifications: N new' were. Owner-only (a guest device doesn't see the owner's stream);
    titles + source + time only, no raw payloads."""
    dev = _device_from_request(request)
    if dev.get("profile") == "trusted_guest":
        return {"ok": False, "error": "not available on this device", "items": []}
    # Read the SAME checkpointed notification set the "N new" counter uses, so the count and this
    # list never disagree (the old 72h window hid notifications older than 3 days that the counter
    # still counted since your last look).
    from .db import connect
    items = []
    try:
        with connect() as con:
            # Keep the log from growing forever: drop rows older than 30 days.
            con.execute("DELETE FROM notifications WHERE received_at < datetime('now','-30 days')")
            # Show only RECENT notifications (last 7 days), newest first, capped , not an ever-growing
            # pile of stale entries (an 11-day-old smoke test should not still be here).
            rows = con.execute(
                "SELECT id, source, app, title, body, urgency, received_at "
                "FROM notifications WHERE received_at > datetime('now','-7 days') "
                "ORDER BY received_at DESC LIMIT 30",
            ).fetchall()
            items = [dict(r) for r in rows]
            # Viewing this list IS reading it: advance the 'new' checkpoint so the "N new" count
            # resets instead of counting the same old notifications as new every time.
            con.execute("INSERT INTO change_checkpoints(domain, last_seen) "
                        "VALUES('notifications', datetime('now')) "
                        "ON CONFLICT(domain) DO UPDATE SET last_seen=datetime('now')")
            con.commit()
    except Exception:
        items = []
    safe = [{"title": n.get("title") or n.get("app") or n.get("source") or "Notification",
             "body": (n.get("body") or "")[:400], "app": n.get("app"), "source": n.get("source"),
             "received_at": n.get("received_at"), "urgency": n.get("urgency")} for n in items]
    return {"ok": True, "items": safe}


@app.post("/device/update/upload")
async def device_update_upload(request: Request) -> dict:
    """An OWNER device uploads a Mongoose package (.zip) to force an update. Device-token auth;
    guests are refused (they can't update the Mac). The zip is validated as a real Mongoose package
    (stage_update_candidate requires pyproject.toml + src/mongoose and rejects unsafe zip paths),
    gated (compileall + pyflakes via check_update_stage), then applied with a brain snapshot +
    rollback on failure, the same machinery the headless /build/ingest uses."""
    import asyncio as _asyncio
    import os as _os
    import tempfile as _tmp
    from pathlib import Path as _Path
    dev = _device_from_request(request)
    if dev.get("profile") == "trusted_guest":
        return {"ok": False, "error": "Only an owner device can update Mongoose."}
    try:
        form = await request.form()
    except Exception:
        return {"ok": False, "error": "Send the .zip as multipart form-data in the 'file' field."}
    up = form.get("file")
    if up is None or not hasattr(up, "read"):
        return {"ok": False, "error": "Choose a Mongoose package .zip to upload."}
    data = await up.read()
    if data[:2] != b"PK":
        return {"ok": False, "error": "That file isn't a .zip package."}
    fd, tmp = _tmp.mkstemp(suffix=".zip", prefix="mongoose-device-update-")
    _os.close(fd)
    staging_path = _Path(tmp)
    staging_path.write_bytes(data)
    from .updater import (stage_update_candidate, check_update_stage,
                          approve_update_stage, apply_update_stage)

    def _stage_check_apply() -> dict:
        try:
            st = stage_update_candidate(str(staging_path))
        except (ValueError, FileNotFoundError) as exc:
            return {"ok": False, "stage": "stage", "error": f"That isn't a valid Mongoose package ({exc})."}
        stage_id = st.get("stage_id")
        if not stage_id:
            return {"ok": False, "stage": "stage", "error": "Could not stage the package."}
        man = st.get("manifest") or {}
        if man.get("ok") is False:
            return {"ok": False, "stage": "manifest", "stage_id": stage_id,
                    "error": "Package manifest check failed: " + "; ".join(man.get("problems") or ["unknown"])}
        chk = check_update_stage(stage_id)
        checks = chk.get("checks", [])
        all_ok = bool(checks) and all(c.get("ok") for c in checks)
        if not all_ok:
            failed = [c.get("name", "?") for c in checks if not c.get("ok")]
            return {"ok": False, "stage": "check", "stage_id": stage_id,
                    "error": f"Package failed validation ({', '.join(failed) or 'no checks ran'}); not applied."}
        approve_update_stage(stage_id)
        rc = apply_update_stage(stage_id, yes=True)
        if rc != 0:
            return {"ok": False, "stage": "apply", "stage_id": stage_id,
                    "error": f"Apply failed (rc={rc}); Mongoose backs up and can roll back."}
        return {"ok": True, "stage": "applied", "stage_id": stage_id,
                "target_version": st.get("target_version"),
                "detail": "Applied. Your data, .env and secrets were preserved and a brain snapshot was taken. "
                          "Mongoose may need a restart for the new code to serve."}
    try:
        return await _asyncio.to_thread(_stage_check_apply)
    finally:
        try:
            staging_path.unlink()
        except Exception:
            pass


@app.get("/cockpit/messages")
def cockpit_messages(request: Request) -> dict:
    """Owner's message threads with guests, for the cockpit. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services import mailbox as mb
    return {"ok": True, "messages": mb.list_thread(participant=mb.OWNER, limit=120),
            "unread": mb.unread_count(mb.OWNER)}


@app.post("/cockpit/messages/send")
async def cockpit_messages_send(request: Request) -> dict:
    """Owner sends a message to a guest (or guest:* broadcast) from the cockpit. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services import mailbox as mb
    payload = await request.json()
    body = str(payload.get("body") or "").strip()
    to = str(payload.get("to") or "").strip()
    if not body or not to:
        return {"ok": False, "error": "both 'to' and 'body' are required"}
    recipient = mb.resolve_recipient_phrase(to) or to
    return mb.send_message(sender=mb.OWNER, recipient=recipient, body=body)


_IRC_GROUP_RULES = (
    "You are mongoose, one participant in a small group chat room (an IRC-style lounge) shared "
    "by the household owner and their guests. Messages arrive as 'Name: text' so you can see "
    "who is speaking. House rules: reply briefly and conversationally, address people by name, "
    "and only add to the conversation when you are addressed, asked something, or can add real "
    "value. You are a participant, not a moderator and not an answering machine. NEVER share "
    "anything from the owner's private conversations, any guest's private thread, stored "
    "memory, or home systems: what happens in this room is only what was said in this room.")


def _irc_sender_name(dev: dict) -> str:
    """The name a device's person shows as in the lounge: their guest profile name first, then
    the display name they chose at pairing, then the device name. Colons are stripped because
    'Name: text' is the attribution format."""
    device_id = str(dev.get("device_id") or "")
    name = ""
    try:
        from .db import connect
        with connect() as con:
            row = con.execute("SELECT mongoose_name FROM guest_profiles WHERE device_id=?",
                              (device_id,)).fetchone()
            if row and (row["mongoose_name"] or "").strip():
                name = row["mongoose_name"].strip()
            if not name:
                row = con.execute("SELECT display_name, name FROM paired_devices WHERE device_id=?",
                                  (device_id,)).fetchone()
                if row:
                    name = (row["display_name"] or "").strip() or (row["name"] or "").strip()
    except Exception:
        pass
    return (name or "someone").replace(":", "")[:40]


_LOUNGE_STARTERS = (
    # trivia
    "trivia time: octopuses have three hearts, and two of them stop beating when they swim. Which animal fact do you wish were true about humans?",
    "quick quiz for the room: what is the only country whose flag is not a rectangle? First right answer earns eternal lounge glory.",
    "trivia: the Eiffel Tower grows about 15 cm taller in summer. What is the best useless fact you are carrying around?",
    "pub quiz question: which planet spins on its side, basically rolling around the sun? No searching!",
    "trivia: honey found in Egyptian tombs was still edible after 3000 years. What food would you trust after 3000 years?",
    "quiz: what do you call a group of flamingos? Closest guess wins.",
    "trivia: Scotland's national animal is the unicorn, chosen on purpose. What should this household's national animal be?",
    "question for the room: what is the smallest country in the world, and would you move there?",
    # food and travel
    "food question: what is the one dish you would cook to impress someone, and has it ever actually worked?",
    "travel corner: best place you have ever been that nobody else seems to have heard of?",
    "tonight's icebreaker: breakfast for dinner, yes or no, and what is on the plate?",
    "question for the room: which snack would you defend in court?",
    "travel question: window seat or aisle, and what does that say about you?",
    # music
    "music question: what song can you not skip, no matter what you are in the middle of?",
    "for the room: first album you ever owned, and do you stand by it?",
    "music corner: Freddie Mercury wrote Crazy Little Thing Called Love in the bath in about ten minutes. What is the best idea you have ever had in a bathtub or shower?",
    "question: one concert, any artist, any era, money no object. Where are we going?",
    "for the room: what is your guilty-pleasure song? This is a judgment-free lounge. Mostly.",
    "music quiz: which artist has won the most Grammys ever? Guesses first, then someone can check.",
    "tonight's question: if this household had a theme song that played every time someone walked in, what should it be?",
    "music corner: vinyl, streaming, radio, or whatever is playing in the supermarket. What is your honest main way of finding new music?",
)


def _maybe_lounge_starter() -> None:
    """Maybe let Mongoose add one safe, group-aware lounge starter/reflection.

    The implementation lives in services/lounge_surpriser.py so the shared guest room stays
    isolated from the owner surpriser. It only uses public #mongoose messages and consented
    guest cards, and it may choose silence.
    """
    try:
        from .services.lounge_surpriser import maybe_lounge_surprise
        maybe_lounge_surprise()
    except Exception:
        pass  # a starter/reflection is a nicety; it must never break history polling


@app.get("/device/irc/history")
def device_irc_history(request: Request, since_id: int = 0, limit: int = 60) -> dict:
    """The lounge's recent messages, oldest-first, with parsed senders. since_id makes polling
    incremental: pass the last id you have and only newer lines come back. Any paired device
    (owner or guest) may read the room: it contains only what was said in the room."""
    dev = _device_from_request(request)
    assert dev is not None
    _maybe_lounge_starter()
    from .services.memory import GROUP_CONVERSATION_ID
    from .db import connect
    from .services import lounge_surpriser as _LS
    my_name = _irc_sender_name(dev)
    my_device_id = str(dev.get("device_id") or "")
    limit = max(1, min(int(limit or 60), 200))
    with connect() as con:
        _LS.ensure_tables(con)
        if since_id:
            rows = con.execute(
                "SELECT m.id, m.role, m.content, m.created_at, "
                "lm.device_id AS meta_device_id, lm.sender AS meta_sender "
                "FROM messages m LEFT JOIN lounge_message_meta lm ON lm.message_id=m.id "
                "WHERE m.conversation_id=? AND m.id>? ORDER BY m.id ASC LIMIT ?",
                (GROUP_CONVERSATION_ID, int(since_id), limit)).fetchall()
        else:
            rows = list(reversed(con.execute(
                "SELECT m.id, m.role, m.content, m.created_at, "
                "lm.device_id AS meta_device_id, lm.sender AS meta_sender "
                "FROM messages m LEFT JOIN lounge_message_meta lm ON lm.message_id=m.id "
                "WHERE m.conversation_id=? ORDER BY m.id DESC LIMIT ?",
                (GROUP_CONVERSATION_ID, limit)).fetchall()))
    out = []
    for r in rows:
        content = r["content"] or ""
        if r["role"] == "assistant":
            sender, text = "mongoose", content
        else:
            sender, _, rest = content.partition(": ")
            if rest:
                sender, text = (r["meta_sender"] or sender)[:40], rest
            else:
                sender, text = (r["meta_sender"] or "someone")[:40], content
        mine = False
        if r["role"] != "assistant":
            if r["meta_device_id"]:
                mine = str(r["meta_device_id"]) == my_device_id
            else:
                # Back-compat for old lounge rows created before metadata existed.
                mine = sender == my_name
        out.append({"id": r["id"], "sender": sender, "text": text,
                    "created_at": r["created_at"], "mongoose": r["role"] == "assistant",
                    "mine": mine})
    return {"ok": True, "messages": out}


def _rate_limited(bucket: str, sender_device: str, *, cooldown_s: float, window_s: float,
                   max_per_window: int, cooldown_msg: str, cap_msg: str) -> str | None:
    """Generic per-sender throttle, shared by any guest-triggered action that reaches a real
    person (a poke, a mailbox message to the owner, anything added later): a minimum gap
    between actions, and a cap per rolling window. State lives in app_state keyed by bucket
    so different actions never share or leak into each other's limits. Fails OPEN if state
    can't be read, since a broken throttle should never break the underlying feature."""
    import json as _json_rl
    import time as _time_rl
    from .db import get_app_state, set_app_state
    key = f"rate_limit:{bucket}:{sender_device}"
    now = _time_rl.time()
    try:
        raw = get_app_state(key)
        hist = [t for t in (_json_rl.loads(raw) if raw else []) if now - t < window_s]
    except Exception:
        hist = []
    if hist and now - hist[-1] < cooldown_s:
        wait = int(cooldown_s - (now - hist[-1])) + 1
        return cooldown_msg.format(wait=wait)
    if len(hist) >= max_per_window:
        return cap_msg
    hist.append(now)
    try:
        set_app_state(key, _json_rl.dumps(hist))
    except Exception:
        pass
    return None


def _poke_rate_limited(sender_device: str) -> str | None:
    return _rate_limited(
        "lounge_poke", sender_device, cooldown_s=20.0, window_s=60.0, max_per_window=4,
        cooldown_msg="give it {wait}s before poking again, so it doesn't turn into spam",
        cap_msg="that's enough pokes for a minute, let the room breathe")


def _guest_mailbox_rate_limited(sender_device: str) -> str | None:
    return _rate_limited(
        "guest_mailbox", sender_device, cooldown_s=10.0, window_s=120.0, max_per_window=8,
        cooldown_msg="give it {wait}s before sending another message",
        cap_msg=("that's a lot of messages for a couple of minutes, give it a bit before "
                 "sending more so the owner's inbox doesn't get flooded"))


async def _lounge_poke(sender: str, sender_device: str, target: str) -> str:
    """Resolve a lounge poke target among paired GUEST lounge names only (embedding-grade
    fuzziness via token overlap and prefix), then push a friendly notification. Returns the
    visible room line.

    Deliberately guest-to-guest only: account users (which may include the owner's real
    login) are never resolved here, regardless of fuzzy match. Account-to-account poking
    already exists as its own feature at POST /poke/{target}, which requires an authenticated
    session and respects each user's allow_poke opt-out. An unauthenticated lounge guest
    reaching that same channel would let anyone page the owner's actual phone by saying
    'poke alex', with no session, no consent check, and (before this fix) no rate limit."""
    limited = _poke_rate_limited(sender_device)
    if limited:
        return limited
    from .services.notifications import send_to_phone
    from .db import connect
    tl = target.lower().strip()
    candidates: list[tuple[str, str | None]] = []  # (display, device_id)
    try:
        with connect() as con:
            for r in con.execute("SELECT mongoose_name, device_id FROM guest_profiles "
                                 "WHERE COALESCE(mongoose_name,'') != ''").fetchall():
                # Exclude the sender's OWN device before scoring, not after: with two guests
                # sharing a lounge name, resolving first and self-checking second means the
                # sender (inserted first, tied score) always wins the match and a genuinely
                # different, pokeable person of the same name gets wrongly blocked as "self".
                if r["device_id"] and str(r["device_id"]) == str(sender_device):
                    continue
                candidates.append((str(r["mongoose_name"]),
                                   str(r["device_id"]) if r["device_id"] else None))
    except Exception:
        pass
    def _score(display: str) -> float:
        d = display.lower()
        if d == tl:
            return 1.0
        if d.startswith(tl) or tl.startswith(d):
            return 0.8
        dt, qt = set(d.split()), set(tl.split())
        return 0.6 * len(dt & qt) / max(1, len(qt)) if dt & qt else 0.0
    best = max(candidates, key=lambda c: _score(c[0]), default=None)
    if not best or _score(best[0]) < 0.6:
        return f"couldn't find a guest called '{target}' to poke. say 'poke <name>' with a name from the room"
    display, device_id = best
    # Sender is already excluded from candidates above; this only fires if resolution somehow
    # still lands on the sender (e.g. device_id missing on both sides), a defensive backstop.
    if device_id and str(sender_device) and device_id == str(sender_device):
        return "you cannot poke yourself (tempting as that is)"
    # CRITICAL: device_id is guest_profiles' primary key so it can't be NULL, but SQLite's PK
    # constraint does not forbid an empty STRING, and a falsy device_id here would make the
    # call below pass target_device_ids=None. list_subscriptions treats an explicit None (as
    # opposed to an empty tuple) as "no restriction at all" and returns EVERY subscription,
    # including the owner's real phone, resurrecting exactly the broadcast bug A2 fixed. Never
    # let a falsy device_id fall through to that "no restriction" meaning: refuse instead.
    if not device_id:
        return f"found {display} but can't reach their device right now, try again shortly"
    # Bind the push to the RECIPIENT'S device, not any broader profile: a personal poke must
    # reach one phone, never every guest's (external audit A2).
    try:
        res = await send_to_phone("👆🪿 Poke!",
                                  f"{sender} poked you from the lounge. Come say hi!",
                                  click="/device/launchpad",
                                  target_device_ids=(device_id,),
                                  tags="wave")
    except Exception:
        return f"tried to poke {display}, but the notification didn't go through"
    # Tell the truth about delivery: promising "on its way" when nobody was reached is a lie.
    # send_to_phone returns "sent" as a BOOL (unlike web_push.send_web_push's count); check it
    # as a bool explicitly rather than relying on bool-is-an-int-subclass coincidence.
    if bool((res or {}).get("sent")):
        return f"{sender} poked {display} 👆 (a notification is on its way)"
    return f"{sender} waved at {display} 👋 ({display} has not turned on notifications yet, so they'll see it when they next open the lounge)"


_POKE_INTENT_RE = _re_mb.compile(r"\bpok(?:e|es|ing|ed)\s+@?([a-z][\w '.\-]{0,40})", _re_mb.IGNORECASE)
_POKE_STOP = {"mongoose", "sillygoose", "please", "now", "ok", "okay", "pls", "thx",
              "thanks", "then", "too", "already", "again"}


_POKE_EXPLAIN_MARKERS = (
    "type ", "typing ", "typed ", "types ", "say ", "saying ", "said ", "says ",
    "write ", "writing ", "wrote ", "enter ", "entering ", "message ", "press ", "tap ",
    "if you ", "when you ", "you can ", "you could ", "you just ", "you only ",
    "for example", "e.g", "such as", "instead of ", "try typing", "try to type",
    "try saying", "try writing", "how do", "how to", "how can", "what if",
    "just type", "just say", "just write",
)


def _parse_poke_target(text: str) -> str | None:
    """Pull a poke target out of natural phrasing so poking feels like talking, not a command
    syntax: 'poke Alex', 'mongoose, poke Alex', 'poking Alex!', 'lets poke Alex mongoose?' all
    yield 'Alex'. Returns the cleaned name, or None if there is no poke intent OR the poke is only
    being TALKED ABOUT (e.g. 'try typing poke Alex', 'if you type poke Alex it will work', a quoted
    'poke Alex'), which must not actually fire a poke."""
    text = text or ""
    m = _POKE_INTENT_RE.search(text)
    if not m:
        return None
    prefix = text[max(0, m.start() - 45):m.start()].lower()
    if prefix.rstrip().endswith(("'", '"', "“", "‘")):  # quoted -> being shown, not commanded
        return None
    if any(mk in prefix for mk in _POKE_EXPLAIN_MARKERS):  # explained, not commanded
        return None
    name = _re_mb.sub(r"[!?.,;:]+$", "", m.group(1).strip()).strip()
    toks = name.split()
    while toks and toks[-1].lower().strip("!?.,;:") in _POKE_STOP:
        toks.pop()
    name = " ".join(toks).strip()
    # A real poke target is a name (1-2 words). Rejecting longer captures avoids idioms like
    # "poke fun at things" or "poke around the settings" being read as a poke.
    if not name or len(toks) > 2:
        return None
    return name


def _session_uid(request: Request) -> str:
    try:
        from .session_auth import session_user, SESSION_COOKIE
        return session_user(request.cookies.get(SESSION_COOKIE)) or ""
    except Exception:
        return ""


async def _poke_from_chat(*, sender_name: str, sender_device: str, sender_user_id: str,
                          target: str) -> str | None:
    """Poke a resolved person from ANY chat (private or lounge). Resolves an ACCOUNT user first
    (including the owner) respecting their allow_poke consent, then a guest lounge name. Returns a
    visible result line, or None when `target` matches nobody so the caller falls through to a
    normal reply instead of posting a noisy 'no such person'. Rate limited like the lounge poke.

    Note (posture): this deliberately lets a chat poke reach the owner's account, which the pure
    lounge path forbade. The safeguards that make that acceptable: the recipient's allow_poke
    opt-out is honoured, and pokes are rate limited per sender."""
    if not target:
        return None
    try:
        from .services.auth_accounts import find_user
        acct = find_user(target)
    except Exception:
        acct = None
    if acct and acct.get("user_id") and acct.get("user_id") != (sender_user_id or ""):
        # Rate-limit the ACCOUNT poke here. The guest path below rate-limits itself inside
        # _lounge_poke, so checking here as well would double-count and wrongly block a first poke.
        limited = _poke_rate_limited(sender_device or sender_user_id or sender_name)
        if limited:
            return limited
        if not acct.get("allow_poke"):
            return f"{acct['display_name']} has pokes turned off."
        # Go through poke_service.send_poke, NOT a raw web push: it RECORDS the poke to the pokes feed
        # (which /poke/history serves and the recipient's lounge polls), so a poke reaches someone who
        # is actively in the room even if their phone push is unavailable. It also fires the push.
        try:
            from .services.poke_service import send_poke
            out = await send_poke(from_user_id=(sender_user_id or ""), from_display=sender_name,
                                  to_user_id=acct["user_id"], to_display=acct["display_name"])
        except Exception:
            return f"tried to poke {acct['display_name']}, but it didn't go through"
        pushed = bool((out.get("notification") or {}).get("sent"))
        if pushed:
            return f"{sender_name} poked {acct['display_name']} 👆 (a notification is on its way)"
        # Recorded to their lounge either way; be honest that the phone push specifically did not land.
        return (f"Poked {acct['display_name']} 👆. It'll show in their lounge when they look. Their phone "
                f"push didn't go through this time (they may need notifications allowed and Mongoose added "
                f"to their home screen).")
    # No account matched: hand to the guest lounge resolver, which pokes a matching guest or
    # returns an honest "couldn't find" so a deliberate poke to an absent name gets feedback.
    # (False positives are already screened out by the 1-2 word target rule in _parse_poke_target.)
    return await _lounge_poke(sender=sender_name, sender_device=sender_device, target=target)


@app.post("/device/irc/send")
async def device_irc_send(request: Request) -> dict:
    """Say something in the lounge. The line is stored with the sender's name; mongoose, as a
    participant, decides whether to speak. It replies when addressed, asked, invited into the room,
    or when a first-time guest needs orientation; otherwise it stays quiet. Its turns run in
    guest_mode with a group-aware prompt built only from public #mongoose context, so the owner's
    private memory and every guest's private thread stay out of the shared room."""
    dev = _device_from_request(request)
    assert dev is not None
    body = await request.json()
    text = str(body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "say something first"}
    if len(text) > 2000:
        return {"ok": False, "error": "keep lounge messages under 2000 characters"}
    name = _irc_sender_name(dev)
    device_id = str(dev.get("device_id") or "")
    line = f"{name}: {text}"
    lower = text.lower()
    addressed = "mongoose" in lower or "sillygoose" in lower

    from .services import lounge_surpriser as _LS

    # Idempotent lounge send: the phone stamps a stable client_msg_id per composed message and reuses it
    # on retry (poor GSM loses the ACK, not necessarily the send). A repeat of the same id is dropped
    # here so a retry can NEVER double-post, which is exactly what makes it safe for the phone to keep the
    # message and auto-resend on reconnect instead of clearing it and losing it (the disappeared-message
    # bug). Content is never compared, so two genuinely re-typed identical lines both go through.
    _icmid = str((body or {}).get("client_msg_id") or "").strip()
    if _icmid:
        import time as _irc_t
        _inow = _irc_t.time()
        for _k in [k for k, v in _SEEN_IRC_IDS.items() if _inow - v > 600]:
            _SEEN_IRC_IDS.pop(_k, None)
        if _icmid in _SEEN_IRC_IDS:
            return {"ok": True, "sender": name, "duplicate": True, "last_id": _LS.lounge_max_id()}
        _SEEN_IRC_IDS[_icmid] = _inow

    # Guest music onboarding, from the lounge too: a guest who only ever uses the public room still gets
    # interviewed, delivered PRIVATELY (their thread + a personal push), never asked in the shared room.
    # Backgrounded so a push never blocks the room; self-paced so it can't spam.
    if dev.get("profile") == "trusted_guest":
        import asyncio as _aio_music
        _mt = _aio_music.create_task(onboard_guest_music(dev))
        _GUEST_MUSIC_TASKS.add(_mt)
        _mt.add_done_callback(_GUEST_MUSIC_TASKS.discard)

    # "add (more) detail to <bug id>" appends to THAT bug deterministically, so it can't fall to the
    # model and get mis-routed to a different bug (M42's note landed on R49 that way).
    from .services.bug_workflow import handle_bug_detail_command as _bug_detail_cmd
    _bd = _bug_detail_cmd(text, actor=("owner" if dev.get("profile") == "primary_phone"
                                       else f"guest:{dev.get('device_id')}"))
    if _bd is not None:
        _LS.store_human_message(device_id=device_id, sender=name, text=text, line=line)
        _LS.post_mongoose_line(_bd["reply"], kind="bug")
        return {"ok": True, "sender": name, "replied": True, "reply": _bd["reply"],
                "last_id": _LS.lounge_max_id()}

    # "raise bug" / "report a bug: X" is the designed bug-intake feature; it must work wherever it
    # is typed (owner or guest, lounge or private chat), landing in the owner's Approvals.
    _bug = _maybe_handle_bug_report(text, is_guest=(dev.get("profile") == "trusted_guest"), dev=dev)
    if _bug is not None:
        _LS.store_human_message(device_id=device_id, sender=name, text=text, line=line)
        _LS.post_mongoose_line(_bug["reply"], kind="bug")
        if _bug.get("bug_id"):
            await _notify_owner_of_bug(_bug["bug_id"], _bug["summary"], _bug["reporter_label"])
        return {"ok": True, "sender": name, "replied": True, "reply": _bug["reply"],
                "last_id": _LS.lounge_max_id()}

    # Fortune cookie in the shared room: "make Jo a fortune" / "generate fortune cookie" is delightful
    # and public. Acknowledge instantly, render the A5 PDF in the BACKGROUND (Swift subprocess), then
    # post the sealed card link into the room, so nobody's client freezes while it bakes.
    from .services.intent_resolver import _fortune_cmd as _fortune_cmd_fn
    if _fortune_cmd_fn(text):
        import asyncio as _aio_fort
        from .services.fortune_cookie import maybe_handle_fortune as _mk_fortune
        _LS.store_human_message(device_id=device_id, sender=name, text=text, line=line)
        _is_owner_l = dev.get("profile") == "primary_phone"

        async def _bake_lounge_fortune() -> None:
            try:
                _fort = await _aio_fort.to_thread(_mk_fortune, text, name, _is_owner_l)
                _LS.post_mongoose_line((_fort or {}).get("reply") or "The fortune cookie crumbled.",
                                       kind="fortune")
            except Exception:
                _LS.post_mongoose_line("The fortune cookie crumbled. Try once more?", kind="fortune")

        _tk = _aio_fort.create_task(_bake_lounge_fortune())
        _LOUNGE_REPLY_TASKS.add(_tk)
        _tk.add_done_callback(_LOUNGE_REPLY_TASKS.discard)
        _ack = "🥠 Baking a fortune cookie, one moment..."
        _LS.post_mongoose_line(_ack, kind="fortune")
        return {"ok": True, "sender": name, "replied": True, "reply": _ack,
                "last_id": _LS.lounge_max_id()}

    # "poke Sam" said in the room sends Sam a real push notification, with fuzzy name
    # matching (accounts and guest lounge names, closest token wins) so "poke sammy"
    # still lands. The room gets a visible confirmation line either way.
    _poke_target = _parse_poke_target(text)
    if _poke_target:
        result_line = await _poke_from_chat(sender_name=name, sender_device=device_id,
                                            sender_user_id=_session_uid(request), target=_poke_target)
        if result_line:
            _LS.store_human_message(device_id=device_id, sender=name, text=text, line=line)
            _LS.post_mongoose_line(result_line, kind="poke")
            return {"ok": True, "sender": name, "replied": True, "reply": result_line,
                    "last_id": _LS.lounge_max_id()}
        # target matched nobody: fall through and treat it as a normal lounge line.

    deterministic = _LS.deterministic_reply_for_turn(text=text, sender=name, device_id=device_id)
    if deterministic:
        _LS.store_human_message(device_id=device_id, sender=name, text=text, line=line)
        _LS.post_mongoose_line(deterministic, kind="orientation")
        return {"ok": True, "sender": name, "replied": True, "reply": deterministic,
                "last_id": _LS.lounge_max_id()}

    # Store the human line NOW, before any model work. Two reasons: (1) liveliness , every other
    # participant sees it on their next poll immediately, instead of only after mongoose finishes
    # composing (the local model can take a long time cold, and chat_with_memory persists the user
    # turn only AFTER generation); (2) crash-safety , the line survives even if the reply fails.
    _LS.store_human_message(device_id=device_id, sender=name, text=text, line=line)
    should_model = addressed or _LS.should_model_reply_to(text=text, sender=name, device_id=device_id)
    if should_model:
        # Generate mongoose's reply OFF the request path. A slow/cold model must never freeze the
        # sender's view of the room (their client keeps polling while it composes) or block the HTTP
        # request. The reply lands in group:irc when ready and reaches everyone via their poll.
        _schedule_lounge_model_reply(line=line, device_id=device_id, name=name)
        return {"ok": True, "sender": name, "replied": False, "pending": True,
                "last_id": _LS.lounge_max_id()}
    return {"ok": True, "sender": name, "replied": False, "last_id": _LS.lounge_max_id()}


# Background lounge model turns. Kept in a set so the event loop cannot garbage-collect an
# in-flight task before it posts its reply.
_LOUNGE_REPLY_TASKS: set = set()


def _schedule_lounge_model_reply(*, line: str, device_id: str, name: str) -> None:
    """Compose mongoose's lounge reply out of band and post it into group:irc, where every
    participant's poll picks it up. The human line is already stored by the caller (skip_user_persist),
    so this only adds mongoose's turn. A model failure posts a calm in-room note, never an error."""
    import asyncio

    async def _run() -> None:
        from .services import lounge_surpriser as _LS
        try:
            from .llm import chat_with_memory
            system = _LS.group_system_prompt(_IRC_GROUP_RULES)
            await chat_with_memory(line, conversation_id="group:irc", role="chat",
                                   guest_mode=True, system=system, skip_user_persist=True)
        except Exception:
            try:
                _LS.post_mongoose_line(_LS.model_down_reply(), kind="model_down")
            except Exception:
                pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(_run())
        _LOUNGE_REPLY_TASKS.add(task)
        task.add_done_callback(_LOUNGE_REPLY_TASKS.discard)
    else:  # no running loop (defensive; the handler is async so this path is unexpected)
        asyncio.run(_run())


# client_msg_id -> first-seen epoch, for at-least-once dedup of retried lounge (IRC) sends (10 min TTL),
# so a poor-connection retry can never double-post and the phone can safely keep + resend the message.
_SEEN_IRC_IDS: dict = {}

# In-flight chat turns started by /device/chat and never cancelled on timeout; the phone
# polls /device/chat/poll with the job id. Entries are removed on retrieval and swept by age.
_CHAT_JOBS: dict = {}
# client_msg_id -> {"ts": first-seen epoch, "jid": job id or None}, for at-least-once dedup of
# retried phone chat sends (10 min TTL). The jid reunites a re-send with its ORIGINAL in-flight
# turn so a retry resumes polling instead of dead-ending on "no need to resend".
_SEEN_CHAT_IDS: dict = {}
_CHAT_JOB_TTL_S = 600.0


def _sweep_chat_jobs() -> None:
    import time as _t
    now = _t.time()
    for k in [k for k, v in _CHAT_JOBS.items() if now - v["created"] > _CHAT_JOB_TTL_S]:
        j = _CHAT_JOBS.pop(k, None)
        if j and not j["task"].done():
            j["task"].cancel()  # ten minutes without retrieval: nobody is coming back for it


def _music_scope_for(dev: dict, is_guest: bool) -> str:
    """The music-taste scope for a private chat: OWNER_SCOPE for the owner, else the guest's account
    user_id (so the 40-minute nudge can push to that guest's phone). Resolves the guest's account by
    the device link first, then by their display name. The device-link lookup returned empty for a
    real guest (Sam), which silently skipped his whole music onboarding."""
    from .services.music_taste import OWNER_SCOPE
    if not is_guest:
        return OWNER_SCOPE
    from .services.auth_accounts import account_for_device, find_user
    scope = account_for_device(str(dev.get("device_id") or "")) or ""
    if not scope:
        nm = (dev.get("display_name") or dev.get("name") or "").strip()
        if nm:
            acct = find_user(nm)
            scope = (acct or {}).get("user_id") or "" if acct else ""
    return scope


# Background music-onboarding tasks (lounge path), held so the loop can't GC them mid-push.
_GUEST_MUSIC_TASKS: set = set()


async def onboard_guest_music(dev: dict) -> bool:
    """Continue a GUEST's music-taste onboarding from ANY surface, including the lounge where the
    question must never be asked publicly. If a question is due, deliver it PRIVATELY: drop it into the
    guest's own chat thread and send them a personal push invite. Self-paced + mark-asked prevent spam.
    Best-effort; returns True if a question was delivered. This is what makes a guest who only uses the
    public room still get interviewed (the wiring gap the introspection auditor now guards)."""
    try:
        scope = _music_scope_for(dev, True)
        if not scope:
            return False
        from .services import music_taste as _MT
        if _MT.is_opted_out(scope):
            return False
        _MT.begin_if_new(scope)
        _MT.note_chat_activity(scope)
        mq = _MT.onboarding_nudge(scope, name=(dev.get("display_name") or "").strip())
        if not mq or not mq.get("text"):
            return False
        _MT.mark_asked(mq["facet"], scope)
        try:
            from .services.memory import add_message
            add_message(_guest_conv_id(dev), "assistant", mq["text"])  # into their PRIVATE thread
        except Exception:
            pass
        try:
            from .services.notifications import send_to_phone
            await send_to_phone(
                "🎶 One quick thing",
                "Mongoose would love to know your music taste. Tap to answer in your private chat.",
                target_user_id=scope, click="/device/launchpad#talk",
                log_source="music", log_app="Mongoose")
        except Exception:
            pass
        return True
    except Exception:
        return False


@app.post("/device/chat")
async def device_chat_route(request: Request) -> dict:
    """Text chat from a paired phone/tablet to the brain. Same conversation as the Mac cockpit chat tab
    (DEFAULT_CONVERSATION_ID), so the phone and Mac share a single brain and history. Device-token auth.
    Body: {message: str}. Returns {ok, reply, transcript} where transcript is the user's own message
    echoed back (so the phone can show a conversation thread without a separate history call)."""
    dev = _device_from_request(request)
    payload = await request.json()
    message = str(payload.get("message") or "").strip()
    if not message:
        return {"ok": False, "error": "say something and I'm all ears"}
    from .llm import chat_with_memory
    from .services.memory import DEFAULT_CONVERSATION_ID
    is_guest = (dev.get("profile") == "trusted_guest")
    # Guests get an isolated conversation per device: never the shared DEFAULT_CONVERSATION_ID.
    conv_id = _guest_conv_id(dev) if is_guest else DEFAULT_CONVERSATION_ID

    # Flood guard by IDEMPOTENCY KEY (never by content): on a slow/dropped connection the phone's
    # outbox re-sends a message whose reply was merely lost, and each copy used to create a fresh turn
    # AND a fresh model reply (13x "benchmark results please" in one incident). The phone now stamps a
    # stable id per COMPOSED message and reuses it on every retry, so a re-send is dropped here , while
    # two genuinely re-typed identical messages carry DIFFERENT ids and BOTH go through (no content
    # comparison, so a real repeat is never blocked).
    _cmid = str((payload or {}).get("client_msg_id") or "").strip()
    if _cmid:
        import time as _cid_t
        _cid_now = _cid_t.time()

        def _cid_ts(v):
            return v.get("ts", 0.0) if isinstance(v, dict) else float(v or 0.0)
        for _k in [k for k, v in _SEEN_CHAT_IDS.items() if _cid_now - _cid_ts(v) > 600]:
            _SEEN_CHAT_IDS.pop(_k, None)
        _prev = _SEEN_CHAT_IDS.get(_cmid)
        if _prev is not None:
            # A re-send of the SAME composed message: the phone retried because the first reply was
            # slow or the response was lost (the heavy owner turn routinely outlives the phone's POST
            # timeout, which then queues the send under the SAME id and replays it here). This must
            # NEVER dead-end: reunite the retry with its ORIGINAL turn. If that turn is still
            # generating, hand back its job id so the phone resumes polling; if it already finished,
            # return the real reply (from the job, then from history) so a lost answer still lands.
            _prev_jid = _prev.get("jid") if isinstance(_prev, dict) else None
            _j = _CHAT_JOBS.get(_prev_jid or "")
            if _j is not None:
                _task = _j["task"]
                if not _task.done():
                    return {"ok": True, "transcript": message, "device": dev.get("device_id"),
                            "duplicate": True, "pending": True, "job_id": _prev_jid,
                            "reply": ("Still working on your last message. The answer will appear "
                                      "here as soon as it's ready.")}
                _CHAT_JOBS.pop(_prev_jid, None)
                try:
                    _res = _task.result()
                    _rep = _res.get("reply") or _res.get("response") or _res.get("text") or ""
                except Exception:
                    _rep = ""
                if _rep:
                    return {"ok": True, "transcript": message, "device": dev.get("device_id"),
                            "duplicate": True, "reply": _rep}
            # The original turn already completed and its job was retrieved or swept: surface the
            # last assistant reply from history so a lost response still reaches the phone, instead
            # of the old dead-end "no need to resend" that stranded the owner indefinitely.
            try:
                from .services.memory import recent_messages as _rm
                for _m in reversed(_rm(conv_id, limit=8, token_budget=None)):
                    if _m.get("role") == "assistant" and (_m.get("content") or "").strip():
                        return {"ok": True, "transcript": message, "device": dev.get("device_id"),
                                "duplicate": True, "reply": _m["content"]}
            except Exception:
                pass
            return {"ok": True, "transcript": message, "device": dev.get("device_id"),
                    "duplicate": True,
                    "reply": ("Got it, that one already went through. Pull down to refresh if you "
                              "don't see the reply.")}
        _SEEN_CHAT_IDS[_cmid] = {"ts": _cid_now, "jid": None}

    # Mailbox hook: let either side send a message THROUGH Mongoose with a natural phrase.
    # A guest "tell Alex X" reaches the owner; the owner "tell Jo X" / "message everyone X"
    # reaches a guest. This runs before the LLM so it's deterministic and never hallucinated.
    mb_reply = _maybe_handle_message_send(message, is_guest=is_guest, dev=dev)
    if mb_reply is not None:
        return {"ok": True, "transcript": message, "reply": mb_reply, "device": dev.get("device_id")}

    # Poke hook: "poke Alex" / "mongoose, poke Alex" / "poking Alex!" from PRIVATE chat pokes the
    # named person (account or guest), exactly like the lounge. Deterministic, before the LLM, so
    # it never hallucinates a poke and never misses one phrased casually.
    poke_target = _parse_poke_target(message)
    if poke_target:
        poker_name = dev.get("display_name") or _irc_sender_name(dev) or "you"
        poke_line = await _poke_from_chat(sender_name=poker_name,
                                          sender_device=str(dev.get("device_id") or ""),
                                          sender_user_id=_session_uid(request), target=poke_target)
        if poke_line:
            return {"ok": True, "transcript": message, "reply": poke_line, "device": dev.get("device_id")}
        # matched nobody: fall through to a normal reply.

    # "add detail to <bug id>" (deterministic, never routed to the model which mis-targets the bug).
    from .services.bug_workflow import handle_bug_detail_command as _bug_detail_cmd
    _bd = _bug_detail_cmd(message, actor=("guest:" + str(dev.get("device_id")) if is_guest else "owner"))
    if _bd is not None:
        return {"ok": True, "transcript": message, "reply": _bd["reply"],
                "device": dev.get("device_id"), "bug_id": _bd.get("bug_id") or None}

    # Bug hook: a guest saying "report a bug: X" / "the light isn't working" logs it for the owner's
    # Approvals tab. Deterministic (never hallucinated), guest-only, and it leaks nothing about the owner.
    bug = _maybe_handle_bug_report(message, is_guest=is_guest, dev=dev)
    if bug is not None:
        if bug.get("bug_id"):
            await _notify_owner_of_bug(bug["bug_id"], bug["summary"], bug["reporter_label"])
        return {"ok": True, "transcript": message, "reply": bug["reply"], "device": dev.get("device_id"),
                "bug_id": bug.get("bug_id") or None}

    # Owner capabilities showcase: "what can I do with you?" (the Status-tab button) gets a warm,
    # curated tour instead of the dry readiness report it used to be misrouted to.
    bench = await _maybe_benchmark_answer(message, is_guest=is_guest)
    if bench is not None:
        try:
            from .services.memory import (ensure_conversation as _ecb, add_message as _amb,
                                          sender_attribution as _sab)
            with _sab(dev.get("device_id", ""), dev.get("name", "")):
                _ecb(conv_id)
                _amb(conv_id, "user", message)
                _amb(conv_id, "assistant", bench)
        except Exception:
            pass
        return {"ok": True, "transcript": message, "reply": bench, "device": dev.get("device_id")}

    cap = await _maybe_capabilities_answer(message, is_guest=is_guest)
    if cap is not None:
        # Persist the exchange, or the 4s history poll rebuilds the thread WITHOUT it and the answer
        # "morphs" into a stale earlier reply (this is exactly what happened: an older "what can I do"
        # turn had persisted the readiness report, and the poll re-rendered that).
        try:
            from .services.memory import (ensure_conversation as _ecc, add_message as _amc,
                                          sender_attribution as _sac)
            with _sac(dev.get("device_id", ""), dev.get("name", "")):
                _ecc(conv_id)
                _amc(conv_id, "user", message)
                _amc(conv_id, "assistant", cap)
        except Exception:
            pass
        return {"ok": True, "transcript": message, "reply": cap, "device": dev.get("device_id")}

    # Self-reflection (owner-only): "how are you wired / built / put together" returns a live map of
    # routes/intents/jobs/capabilities plus the self-audit of any missing wires. Mongoose knowing how it
    # is wired, from real code, not a hand-maintained blurb.
    # NOTE: `re` is not a module-level import in this file (every other use is a locally-aliased import).
    # A bare `re.search` here raised NameError for EVERY owner chat turn that reached this line (guests
    # skip it via `not is_guest`), 503-ing the owner's send. The phone then replayed the same id into the
    # idempotency guard and got stranded on "still working, no need to resend": the exact "my private
    # chat is dead but Sam's works" bug. Import re locally so the owner path actually runs.
    import re as _re_wired
    if not is_guest and _re_wired.search(
            r"\bhow (?:are|were) you (?:wired|built|put together|structured)\b|\bwhat.?s your wiring\b"
            r"|\bhow do you work internally\b|\bself[- ]audit\b|\bhow are you plumbed\b", message or "", _re_wired.I):
        try:
            from .services.introspection import summary_text
            return {"ok": True, "transcript": message, "reply": summary_text(), "device": dev.get("device_id")}
        except Exception:
            pass

    # Guest delights: "what can I do", "surprise me", "fun fact", "tell me something" get a warm,
    # deterministic, guest-safe response (curated content, no owner data, no model needed). This makes
    # a guest's first interaction feel magical and never hallucinates.
    if is_guest:
        delight = _maybe_guest_delight(message)
        if delight is not None:
            # Persist it, or the 4s history-rebuild poll (which redraws the thread from server storage)
            # wipes the delight out from under the reader , the "fun fact vanished when new messages
            # arrived" bug that breaks the chat-room illusion. Same fix the bench/capabilities paths use.
            try:
                from .services.memory import (ensure_conversation as _ecd, add_message as _amd,
                                              sender_attribution as _sad)
                with _sad(dev.get("device_id", ""), dev.get("name", "")):
                    _ecd(conv_id)
                    _amd(conv_id, "user", message)
                    _amd(conv_id, "assistant", delight)
            except Exception:
                pass
            return {"ok": True, "transcript": message, "reply": delight, "device": dev.get("device_id"),
                    "delight": True}

    # Music interview (owner AND guests): keep the onboarding clock ticking on real chat and capture
    # an answer to any pending music question, so a normal chat reply doubles as the answer.
    _music_scope = _music_scope_for(dev, is_guest)
    if _music_scope:
        try:
            from .services import music_taste as _MT
            from .llm import _looks_like_command_or_confirm as _cmdish
            _MT.begin_if_new(_music_scope)
            _MT.note_chat_activity(_music_scope)
            # Never file a real command / confirm token as a music answer (this phone path had none of
            # the guards the chat path got, so it was the bypass that still corrupted the profile).
            if not _cmdish(message):
                _MT.capture_answer(message, _music_scope)
        except Exception:
            pass

    try:
        # Phones abort a hung request after ~15-30s, while the local model can take longer on a cold
        # or busy turn (the desktop web UI tolerates this; a phone does not, which is why a guest "times
        # out even though nothing changed"). Bound the wait so the guest always gets a real HTTP
        # response within budget rather than a dead socket. If the model is still working, say so
        # honestly instead of hanging.
        import asyncio as _asyncio
        import os as _os_b
        try:
            _BUDGET_S = float(_os_b.getenv("MONGOOSE_GUEST_CHAT_BUDGET_S", "25"))
        except (TypeError, ValueError):
            _BUDGET_S = 25.0
        from .services.memory import sender_attribution, add_message as _persist, ensure_conversation as _ec
        # Persist the user's message BEFORE any model work. A live incident lost a
        # message entirely: the old wait_for CANCELLED the whole turn on timeout, and
        # since persistence lived inside the cancelled turn, a refresh two minutes
        # later showed nothing. The message must survive any timeout, crash, or
        # refresh from the moment the server has it. ensure_conversation first: guest
        # threads are created lazily, and persisting into a missing conversation row
        # would fail the foreign key and route a healthy turn into the brain-down path.
        with sender_attribution(dev.get("device_id", ""), dev.get("name", "")):
            _ec(conv_id)
            _persist(conv_id, "user", message)
            # Start the turn as a BACKGROUND task and never cancel it: wait_for's
            # cancellation both destroyed in-flight generation (the "ask again" advice
            # restarted from cold) and made the timeout reply a lie (nothing was still
            # thinking; the work was gone). The task inherits attribution via the
            # task's context copy.
            job = _asyncio.create_task(chat_with_memory(
                message, conversation_id=conv_id, role="chat", guest_mode=is_guest,
                skip_user_persist=True))
        import time as _time_cj
        jid = _secrets.token_hex(8)
        _CHAT_JOBS[jid] = {"task": job, "created": _time_cj.time(),
                           "device_id": dev.get("device_id", "")}
        # Bind the idempotency id to THIS job so a retried send (the phone's outbox replays the same
        # id when a slow turn outlives its POST) resumes polling the same turn instead of dead-ending.
        if _cmid:
            _SEEN_CHAT_IDS[_cmid] = {"ts": _CHAT_JOBS[jid]["created"], "jid": jid}
        done, _pending = await _asyncio.wait({job}, timeout=_BUDGET_S)
        if not done:
            # Honest pending: the work CONTINUES server-side; the phone polls
            # /device/chat/poll every couple of seconds until the reply lands. Even
            # with no polling at all, the finished turn persists the reply, so a plain
            # refresh shows both sides in history.
            return {"ok": True, "transcript": message, "device": dev.get("device_id"),
                    "pending": True, "job_id": jid,
                    "reply": ("Still working on it. This one is taking longer than "
                              "usual; the answer will appear here as soon as it's "
                              "ready.")}
        _CHAT_JOBS.pop(jid, None)
        # A failed turn raises here and reaches the brain_down handler below, which owes
        # the person a warm, non-technical reply (and the owner, not a guest, the detail).
        result = job.result()
        reply = result.get("reply") or result.get("response") or result.get("text") or ""
        # If this participant has unread messages, gently surface a count after the reply.
        try:
            from .services import mailbox as _mb
            participant = f"guest:{dev['device_id']}" if is_guest else _mb.OWNER
            n = _mb.unread_count(participant)
            if n:
                reply = f"{reply}\n\n(You have {n} new message{'s' if n != 1 else ''} via Mongoose: say \"read my messages\".)"
        except Exception:
            pass
        # Occasionally ask the OWNER (never a guest) for training/nutrition data so the body features
        # stay real. Paced so it's rare, not a nag; uploading from the phone satisfies it.
        if not is_guest:
            try:
                from .services import body_data_nudge as _bdn
                _nudge = _bdn.due_nudge()
                if _nudge:
                    reply = f"{reply}\n\n{_nudge['ask']}"
                    _bdn.mark_asked(_nudge["reason"])
            except Exception:
                pass
        # Music interview (everyone): now and then slip the next music question onto the reply. It
        # self-paces (escalating schedule, chat-gated) and marks itself asked, which arms the
        # 40-minute "come answer" nudge if they leave it hanging.
        if _music_scope:
            try:
                from .services import music_taste as _MT
                _mq = _MT.onboarding_nudge(_music_scope, name=(dev.get("display_name") or "").strip())
                if _mq and _mq.get("text"):
                    reply = f"{reply}\n\n{_mq['text']}"
                    # Persist the question as its own assistant message too. chat_with_memory already
                    # saved the base reply WITHOUT this appendix, so without this the question lived only
                    # in the transient HTTP body: a later history refresh dropped it, yet the facet was
                    # marked asked, so the 40-minute "come answer" push landed on an empty chat (the
                    # reported Sam bug). Saving it here guarantees the pushed question is really there.
                    try:
                        from .services.memory import add_message as _persist_mq
                        _persist_mq(conv_id, "assistant", _mq["text"])
                    except Exception:
                        pass
                    # Mark asked ONLY here, where the question is actually delivered (appended to the
                    # reply the owner sees AND saved to the thread), so the answer-capture window can't
                    # open on an undelivered one.
                    _MT.mark_asked(_mq["facet"], _music_scope)
            except Exception:
                pass
        return {"ok": True, "transcript": message, "reply": reply, "device": dev.get("device_id")}
    except Exception as exc:
        # A raw exception string ("All connection attempts failed") on a guest's phone reads like
        # THEIR wifi broke and tells them nothing. Say what actually happened, warmly and
        # honestly, in Mongoose's voice. ok stays True so every client renders it as a normal
        # bubble; brain_down + error_detail carry the truth for the owner's diagnostics. The
        # user's message IS saved (persist-first happens before any model work), so the copy
        # says so, and never tells them to retype it.
        _is_guest = str(dev.get("profile") or "").endswith("guest") or str(dev.get("device_type") or "").startswith("guest")
        return {"ok": True, "transcript": message, "device": dev.get("device_id"),
                # Guests never receive backend wording, local URLs, or exception names; the
                # owner keeps the detail (external audit, finding 6).
                "brain_down": True, **({} if _is_guest else {"error_detail": str(exc)}),
                "reply": ("My brain isn't answering right now (the local model on the Mac is "
                          "asleep or busy). Nothing is wrong on your end and your message is "
                          "saved. Ask me again in a minute, and if it keeps happening, "
                          "mention it to your host.")}


@app.post("/device/voice")
async def device_voice_route(request: Request) -> dict:
    """Voice push-to-talk from a paired phone.

    Hardening notes:
    - browser audio MIME varies by iOS/Android/browser, so use the declared MIME to pick an extension;
    - Whisper transcription is synchronous/subprocess-backed, so run it off the event loop;
    - bound both transcription and model waits so one stuck voice turn does not freeze every phone/cockpit
      request on a network glitch or cold local model.
    """
    dev = _device_from_request(request)
    _MAX_AUDIO = 25 * 1024 * 1024
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > _MAX_AUDIO:
        return {"ok": False, "error": "audio too large (max 25 MB)"}
    body = await request.body()
    if not body:
        return {"ok": False, "error": "no audio received"}
    if len(body) > _MAX_AUDIO:
        return {"ok": False, "error": "audio too large (max 25 MB)"}

    import asyncio as _asyncio
    import os as _os
    import tempfile
    from pathlib import Path as _Path
    from .voice_benchmark import _whisper_transcribe

    def _float_env(name: str, default: float) -> float:
        try:
            return float(_os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    # Browser audio MIME varies (iOS Safari gives mp4/aac, Chrome gives webm/opus). Pick the file
    # extension from what the browser actually declared, so Whisper gets a correctly-named file.
    raw_mime = (request.headers.get("x-audio-mime")
                or request.headers.get("content-type") or "audio/webm")
    mime = raw_mime.split(";", 1)[0].strip().lower()
    ext = {
        "audio/webm": "webm",
        "audio/ogg": "ogg",
        "audio/opus": "opus",
        "audio/mp4": "m4a",
        "audio/aac": "aac",
        "audio/mpeg": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
    }.get(mime, "webm")
    stt_timeout = _float_env("MONGOOSE_DEVICE_VOICE_STT_TIMEOUT_S", 75.0)
    brain_timeout = _float_env("MONGOOSE_DEVICE_VOICE_LLM_TIMEOUT_S", 25.0)

    with tempfile.TemporaryDirectory() as d:
        clip = _Path(d) / f"clip.{ext}"
        clip.write_bytes(body)
        try:
            stt = await _asyncio.wait_for(
                _asyncio.to_thread(_whisper_transcribe, clip),
                timeout=stt_timeout,
            )
        except _asyncio.TimeoutError:
            return {"ok": False, "transient": True, "stage": "transcription",
                    "error": "Voice transcription is taking too long. Try again, or type it for now."}
        except Exception as exc:
            return {"ok": False, "transient": True, "stage": "transcription",
                    "error": f"Voice transcription failed: {exc}"}

    if stt is None:
        return {"ok": False, "whisper_missing": True,
                "error": "Voice transcription isn't set up yet. "
                         "Open Mongoose on the Mac → Settings → Voice transcription → Install."}
    _, transcript = stt
    transcript = (transcript or "").strip()
    if not transcript:
        return {"ok": True, "transcript": "", "reply": "(I didn't catch that: try again.)"}

    from .llm import chat_with_memory
    from .services.memory import DEFAULT_CONVERSATION_ID
    try:
        _is_guest_v = dev.get("profile") == "trusted_guest"
        _conv_v = _guest_conv_id(dev) if _is_guest_v else DEFAULT_CONVERSATION_ID
        # Reply ASYNCHRONOUSLY, exactly like the text chat. A 20s clip already spent ~10s in whisper,
        # and the local model can be cold/slow; blocking the whole request on it pushed voice past the
        # client and Cloudflare timeouts, whose 5xx shows on the phone as "bad voice response". Persist
        # the transcript, kick the reply as a background job, and let the phone poll /device/chat/poll.
        from .services.memory import (sender_attribution as _sa_v, add_message as _persist_v,
                                      ensure_conversation as _ec_v)
        with _sa_v(dev.get("device_id", ""), dev.get("name", "")):
            _ec_v(_conv_v)
            _persist_v(_conv_v, "user", transcript)
            job = _asyncio.create_task(chat_with_memory(
                transcript, conversation_id=_conv_v, role="chat",
                guest_mode=_is_guest_v, skip_user_persist=True))
        import time as _tv
        jid = _secrets.token_hex(8)
        _CHAT_JOBS[jid] = {"task": job, "created": _tv.time(), "device_id": dev.get("device_id", "")}
        # Let a warm turn finish inline (snappy), else hand back a job id the phone polls , never
        # block the request on a cold model.
        done, _pend = await _asyncio.wait({job}, timeout=min(brain_timeout, 6.0))
        if done:
            _CHAT_JOBS.pop(jid, None)
            result = job.result()
            reply = result.get("reply") or result.get("response") or result.get("text") or ""
            return {"ok": True, "transcript": transcript, "reply": reply,
                    "device": dev.get("device_id"), "audio_mime": mime}
        return {"ok": True, "transcript": transcript, "pending": True, "job_id": jid,
                "reply": "…", "device": dev.get("device_id"), "audio_mime": mime}
    except Exception as exc:
        # Same class as the chat route: never show a guest a raw exception. They spoke, they were
        # heard (transcript proves it); the brain just didn't answer.
        _is_guest_v = str(dev.get("profile") or "").endswith("guest") or str(dev.get("device_type") or "").startswith("guest")
        return {"ok": True, "transcript": transcript, "brain_down": True,
                **({} if _is_guest_v else {"error_detail": str(exc)}),
                "reply": ("I heard you, but my brain isn't answering right now (the local model "
                          "on the Mac is asleep or busy). Try me again in a minute.")}


@app.get("/device/chat/history")
async def device_chat_history_route(request: Request, limit: int = 30) -> dict:
    """Recent conversation history for the phone chat view. A guest device gets ITS OWN isolated
    thread (guest:<device_id>); an owner device gets the shared main conversation. This must match
    the conversation the chat WRITE path uses, or a guest would see the owner's history (a privacy
    leak) and not its own (a recovery failure). Device-token auth."""
    dev = _device_from_request(request)
    from .services.memory import DEFAULT_CONVERSATION_ID
    from .db import connect
    is_guest = (dev.get("profile") == "trusted_guest")
    conv_id = _guest_conv_id(dev) if is_guest else DEFAULT_CONVERSATION_ID
    with connect() as con:
        rows = con.execute(
            "SELECT id, created_at, role, content, sender_device_id, sender_label FROM messages "
            "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (conv_id, min(limit, 60))).fetchall()
    msgs = []
    for r in reversed(rows):
        # The monotonic row `id` is the AUTHORITATIVE render order + dedupe key for the client. Without
        # it the phone had no stable key to merge (fetched history + optimistic-sent + polled) by, so
        # bubbles visibly shuffled between polls. created_at is advisory only (tie-prone across
        # concurrent turns); the client must order by id, never by created_at.
        m = {"id": r["id"], "created_at": r["created_at"], "role": r["role"], "content": r["content"] or ""}
        # Label messages another device authored, so a shared thread can never again show
        # someone else's words as silently indistinguishable from the reader's own.
        sid = r["sender_device_id"]
        if sid and sid != dev.get("device_id"):
            m["sender"] = r["sender_label"] or "another device"
        msgs.append(m)
    return {"ok": True, "messages": msgs, "conversation": conv_id}


@app.get("/device/self/name")
async def device_self_name_get(request: Request) -> dict:
    """The device's own current display name (empty if never set), so the launchpad can
    prompt exactly the unnamed devices and nobody else."""
    dev = _device_from_request(request)
    return {"ok": True, "display_name": (dev.get("display_name") or "").strip(),
            "name": (dev.get("name") or "").strip(), "profile": dev.get("profile")}


@app.post("/device/self/name")
async def device_self_name(request: Request) -> dict:
    """A paired device sets its own display name (register B2: a re-paired phone had no
    name saved, so IRC rendered its person as 'someone'). The launchpad prompts when the
    name is missing; this saves it. Device-token auth; a device may only name ITSELF."""
    dev = _device_from_request(request)
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid JSON body"}
    name = str(payload.get("name") or "").strip()[:40]
    if not name:
        return {"ok": False, "error": "a name is required"}
    from .devices import set_device_display_name
    r = set_device_display_name(dev["device_id"], name)
    return {"ok": bool(r.get("ok")), "display_name": name}


@app.get("/device/chat/poll")
async def device_chat_poll(request: Request, job_id: str = "") -> dict:
    """Retrieve a chat turn that outlived the send request's wait budget. The turn was
    never cancelled: it kept generating server-side, and this returns its reply when done.
    Only the device that started a job may poll it. Finished jobs are removed on first
    retrieval; unclaimed jobs are swept after ten minutes."""
    dev = _device_from_request(request)
    _sweep_chat_jobs()
    j = _CHAT_JOBS.get(job_id or "")
    if j is None:
        return {"ok": False, "done": True,
                "error": "unknown or expired job; check the chat history, the reply may already be there"}
    if j.get("device_id") != dev.get("device_id"):
        return {"ok": False, "done": True, "error": "not your job"}
    task = j["task"]
    if not task.done():
        import time as _t
        return {"ok": True, "done": False, "elapsed": round(_t.time() - j["created"], 1)}
    _CHAT_JOBS.pop(job_id, None)
    try:
        result = task.result()
    except Exception as exc:
        return {"ok": False, "done": True, "error": f"chat failed: {str(exc)[:200]}"}
    reply = result.get("reply") or result.get("response") or result.get("text") or ""
    # Return the persisted assistant row's monotonic id + created_at (best-effort) so the client can
    # reconcile the optimistic reply bubble to its authoritative server row and order/dedupe by id,
    # exactly like the history payload. If the row can't be located, the client keeps the optimistic
    # bubble until the next history poll supplies the id.
    out = {"ok": True, "done": True, "reply": reply}
    conv_id = result.get("conversation_id") or result.get("conversation")
    if reply and conv_id:
        try:
            from .db import connect
            with connect() as con:
                row = con.execute(
                    "SELECT id, created_at FROM messages WHERE conversation_id = ? AND role = 'assistant' "
                    "AND content = ? ORDER BY id DESC LIMIT 1", (conv_id, reply)).fetchone()
            if row:
                out["id"] = row["id"]
                out["created_at"] = row["created_at"]
        except Exception:
            pass
    return out


@app.post("/device/notifications/ingest")
async def device_notifications_ingest(request: Request) -> dict:
    """Scoped notification relay endpoint for paired devices (Android companion, etc).
    Accepts a notification payload from a device using its device token: no master API token needed.
    Body: {title: str, text: str, app: str, package: str?, timestamp: str?, priority: 'normal'|'high'}.
    Stores the notification as a device event for the brain to surface in summaries.
    This is the clean device-scoped alternative to the broad /notifications/ingest route."""
    dev = _device_from_request(request)
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid JSON body"}
    title = str(payload.get("title") or "").strip()
    text = str(payload.get("text") or "").strip()
    app_name = str(payload.get("app") or "").strip()
    package = str(payload.get("package") or "").strip()
    priority = str(payload.get("priority") or "normal").strip()
    privacy = str(payload.get("privacy") or "medium").strip().lower()
    if privacy not in {"low", "medium", "high", "secret"}:
        privacy = "medium"
    if not (title or text):
        return {"ok": False, "error": "provide at least title or text"}
    from .devices import record_device_event
    event_data = {"title": title, "text": text, "app": app_name,
                  "package": package, "priority": priority,
                  "device_id": dev.get("device_id"), "device_name": dev.get("name")}
    record_device_event(dev.get("device_id", ""), "notification_relayed", event_data, privacy)
    # Also push to the notification inbox so it surfaces in search/what-changed. Regression: this
    # used to call the async ingest function with keyword args meant for a different API, swallow the
    # TypeError, and therefore "accept" phone notifications that were never searchable.
    inbox_result = {"inserted": False, "error": "not_attempted"}
    try:
        from .services.notifications import NotificationIn, ingest_notification
        inbox_result = await ingest_notification(NotificationIn(
            source="device",
            app=app_name or package or "device",
            title=title or app_name or "Notification",
            body=text,
            privacy=privacy,
            urgency="high" if priority.lower() in {"high", "critical", "max"} else "normal",
            device=str(dev.get("name") or dev.get("device_id") or "device"),
            raw={"package": package, "device_id": dev.get("device_id"), "device_name": dev.get("name")},
        ))
    except Exception as exc:
        inbox_result = {"inserted": False, "error": str(exc)[:200]}
    return {"ok": True, "ingested": True, "device": dev.get("device_id"),
            "notification_inbox": inbox_result}


@app.post("/cockpit/conversation/rollover")
async def cockpit_conversation_rollover(request: Request) -> dict:
    """Owner remedy for a contaminated main thread: archive the ENTIRE current shared
    conversation under a dated id and start fresh. Exists because a mis-paired device's
    messages from before attribution existed cannot be selected individually; the archive
    preserves everything (nothing is deleted) while the brain thread starts clean.
    Cockpit-nonce gated: owner surface only."""
    _require_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    from .services.memory import rollover_main_conversation
    return rollover_main_conversation(str(body.get("reason") or ""))


@app.post("/cockpit/devices/pair-code")
async def cockpit_devices_pair_code(request: Request) -> dict:
    """Mint a short-lived pairing code for a new device. Nonce-gated.
    trusted_guest QR uses the DDNS address so the friend can scan from outside home."""
    _require_cockpit_nonce(request)
    from .devices import create_pairing_code, pairing_qr_svg
    payload = await request.json()
    profile = payload.get("profile", "primary_phone")
    # Allow trusted_guest in addition to primary_phone and home_tablet
    if profile not in ("primary_phone", "home_tablet", "trusted_guest"):
        profile = "primary_phone"
    result = create_pairing_code(profile)
    # REGRESSION FIX: this used to prefer DDNS unconditionally for every profile ("you might
    # scan from the office"), but pairing the OWNER's own phone/tablet is almost always done
    # standing right next to the Mac, about to scan immediately on the same WiFi. Most
    # consumer routers don't support NAT loopback/hairpin routing, so a LAN device reaching
    # for the router's own public DDNS hostname gets a flat connection refusal, exactly what
    # this looked like. trusted_guest genuinely needs DDNS (a friend scanning from elsewhere
    # is the whole point); primary_phone/home_tablet now default LAN-first with DDNS offered
    # as an explicit fallback link rather than silently taking over the primary QR.
    ddns_base = _guest_reachable_url(request)  # reuses the same DDNS-or-LAN helper
    lan_scheme = "https" if request.url.scheme == "https" else "http"
    lan_host = _phone_reachable_host(request)
    lan_base = f"{lan_scheme}://{lan_host}"
    if profile == "trusted_guest":
        is_ddns = ddns_base != lan_base
        chosen_base = ddns_base if is_ddns else lan_base
    else:
        chosen_base = lan_base
        is_ddns = False
        if ddns_base and ddns_base != lan_base:
            result["remote_pair_url"] = f"{ddns_base}/pair?code={result['code']}"
            result["remote_pair_hint"] = "pairing away from home? use this link instead"
    pair_url = f"{chosen_base}/pair?code={result['code']}"
    result["ddns_used"] = is_ddns
    if not is_ddns and profile != "trusted_guest":
        result["ddns_hint"] = None  # no hint for owner on LAN: they're on their home network
    result["pair_url"] = pair_url
    note = _pairing_reachability_note()
    if note and profile != "trusted_guest":
        # Same fix as the /install/pair-code path: a scannable-looking QR that already has a
        # known reason to fail is worse than none. trusted_guest is untouched here since its
        # reachability question is DDNS, not this Mac's LAN socket.
        result["qr_svg"] = ""
        result["qr_blocked"] = True
        result["reachability_warning"] = note
        result["reachability_diagnosis"] = _phone_reachability_diagnosis()
        # DEFENSE IN DEPTH, matching /install/pair-code: only fires on explicit request
        # (payload.auto_repair), this route is already nonce-gated owner-only.
        if bool(payload.get("auto_repair")):
            result["auto_repair"] = _repair_phone_reachability("cockpit_pair_code")
    else:
        result["qr_svg"] = pairing_qr_svg(pair_url)
    if profile == "trusted_guest":
        # Guests reach Mongoose from OUTSIDE the home, so the link must be both reachable (DDNS) and
        # secure (HTTPS). A http:// link over the open internet exposes the pairing + their messages.
        # Surface the real state so the owner sorts it before handing a guest a link.
        from .acme_tls import certificate_status as _cert_status
        is_https = chosen_base.startswith("https://")
        cert = _cert_status()
        if not result.get("ddns_used"):
            result["ddns_hint"] = ("DDNS not configured: a guest will only reach Mongoose on your home "
                                   "network. Set up DuckDNS in Settings to enable remote access.")
            result["guest_blocker"] = "no_remote_access"
        elif not is_https:
            # Reachable remotely but only over http, warn loudly and tell them how to fix it.
            result["guest_blocker"] = "insecure_http"
            result["https_hint"] = (
                "This guest link is remote but NOT encrypted (http). Their messages and pairing "
                "would travel in the clear. One click, no terminal: Setup -> Remote access -> "
                "Get a trusted certificate.")
        else:
            result["https_ok"] = True
            if cert.get("expires"):
                result["cert_expires"] = cert["expires"]
    return result


@app.get("/cockpit/guests/invites")
def cockpit_list_guest_invites(request: Request) -> dict:
    """List guest invites (newest first, no raw tokens) so the owner can find and revoke a
    misdirected or unwanted one. Nonce-gated. Includes the same readiness snapshot used by
    the setup chase, but never the raw invite token."""
    _require_cockpit_nonce(request)
    from .guest_invites import guest_invite_prerequisites, list_invites
    return {"ok": True, "readiness": guest_invite_prerequisites(), "invites": list_invites()}


@app.post("/cockpit/guests/invites/{invite_id}/revoke")
def cockpit_revoke_guest_invite(invite_id: str, request: Request) -> dict:
    """Revoke a guest invite by id. If it was already claimed, the guest device it created is revoked
    too, so 'cancel this invite' ends access whether or not the guest already came in. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .guest_invites import revoke_invite
    return revoke_invite(invite_id)


@app.post("/cockpit/guests/reset-password")
async def cockpit_reset_guest_password(request: Request) -> dict:
    """Owner-authorised password reset for a person's account (guest or member). The host triggers it
    from Cockpit; it mints a fresh typeable passphrase, sets it, and returns it ONCE to relay. This is
    the reset authority for a local-first app with no email: the host resets, hands over the new
    password, and the person signs in. Nonce-gated (owner). Body: {user_id} or {username}."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .services import auth_accounts as AA
    uid = str(body.get("user_id") or "").strip()
    if not uid:
        who = str(body.get("username") or "").strip()
        acct = AA.find_user(who) if who else None
        uid = (acct or {}).get("user_id", "") if acct else ""
    if not uid:
        return {"ok": False, "error": "no such account (give a user_id or username)"}
    res = AA.reset_password(uid)
    if res.get("ok"):
        try:
            from .db import audit
            audit("account", "password_reset", True, route="cockpit_reset_guest_password",
                  payload={"user_id": uid, "username": res.get("username")})
        except Exception:
            pass
    return res


@app.get("/cockpit/devices/pair-status")
def cockpit_devices_pair_status(request: Request, code: str = "") -> dict:
    """Has this just-issued pairing code been claimed yet? Lets the cockpit drop the QR the
    instant a device pairs, so a single-use code never keeps showing a scannable QR after it's
    been used. Read-only lifecycle state; the code is already visible to this authed cockpit."""
    from .devices import pairing_code_status
    return pairing_code_status(code)


@app.post("/cockpit/devices/revoke")
async def cockpit_devices_revoke(request: Request) -> dict:
    """Revoke a paired device immediately. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .devices import revoke_device
    payload = await request.json()
    return revoke_device(payload.get("device_id", ""))


@app.post("/cockpit/devices/merge")
async def cockpit_devices_merge(request: Request) -> dict:
    """Fold a duplicate 'same person' device into a surviving one. Owner-only (nonce-gated).
    Accident-hardened in devices.merge_devices: same profile only, live target only, and the
    caller must echo both device names exactly. Nothing is deleted; the source is revoked."""
    _require_cockpit_nonce(request)
    from .devices import merge_devices
    payload = await request.json()
    return merge_devices(
        payload.get("source_id", ""), payload.get("target_id", ""),
        confirm_source_name=payload.get("confirm_source_name", ""),
        confirm_target_name=payload.get("confirm_target_name", ""))


@app.post("/cockpit/guests/invite")
async def cockpit_guests_invite(request: Request) -> dict:
    """Create a no-QR trusted-guest invite link.

    Unlike the 5-minute QR pairing code, this is designed for a friend: default 5 days,
    DDNS + HTTPS required by default, owner-side guest label stored internally only, and
    a minimum Mongoose client version defaulting to 1.1.
    """
    _require_cockpit_nonce(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    from .guest_invites import create_guest_invite
    return create_guest_invite(
        owner_label=payload.get("owner_label") or payload.get("guest_label") or "",
        valid_days=payload.get("valid_days") or payload.get("days") or 5,
        min_version=payload.get("min_version") or "1.1",
        suggested_name=payload.get("suggested_name"),
    )


@app.get("/cockpit/guests/readiness")
def cockpit_guests_readiness() -> dict:
    """Read-only: are the prerequisites for guest URL invites in place (HTTPS + Dynamic DNS + remote
    access)? This is the SAME check create_guest_invite enforces, so the setup tab can chase exactly
    what invites demand. No invites or tokens are returned."""
    from .guest_invites import guest_invite_prerequisites
    return guest_invite_prerequisites()


@app.get("/guest/invite", response_class=HTMLResponse)
def guest_invite_tokenless() -> HTMLResponse:
    """The valid invite page deliberately strips the secret token from the address bar
    (history.replaceState to /guest/invite). A guest who refreshes, reopens the tab, or
    swipes back then lands HERE, and used to get raw JSON 404 (external audit, finding 1,
    the one journey-blocker). Say what happened and what to do, warmly."""
    return HTMLResponse(content=(
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Mongoose invite</title></head>"
        "<body style='font-family:-apple-system,system-ui,sans-serif;background:#16140F;"
        "color:#ECE6DA;display:flex;min-height:100vh;align-items:center;justify-content:center;"
        "margin:0;padding:24px;text-align:center'><div style='max-width:420px'>"
        "<h1 style='font-size:22px'>This page hid the invite link for safety</h1>"
        "<p style='color:#9C9486;line-height:1.5'>Invite links are secret, so the page removes "
        "them from the address bar once opened. If you already finished joining, open Mongoose "
        "from your home screen. If not, open the original invite link from your host's message, "
        "or ask them for a fresh one.</p></div></body></html>"))


@app.get("/guest/invite/{token}", response_class=HTMLResponse)
def guest_invite_page(token: str) -> HTMLResponse:
    """Public guest invite landing page. The token is the capability; no owner data is embedded.
    no-referrer keeps the token out of the Referer header on any request the page makes after
    landing; no-store keeps the tokenised URL out of browser/proxy disk caches."""
    from .guest_invites import invite_status
    from .cockpit import guest_invite_html
    return HTMLResponse(
        content=guest_invite_html(token, invite_status(token)),
        headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store, max-age=0"},
    )


@app.post("/guest/invite/{token}/claim")
async def guest_invite_claim(token: str, request: Request) -> dict:
    """Public claim endpoint for a guest invite. Creates a scoped trusted_guest device token once."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    from .guest_invites import claim_guest_invite
    return claim_guest_invite(token, payload)



@app.post("/guest/invite/{token}/delete")
async def guest_invite_delete(token: str) -> dict:
    """Public: the invite holder can delete their own invitation from the landing page.
    The token is the capability; the operation is idempotent and reveals nothing else."""
    from .guest_invites import delete_invite_by_token
    return delete_invite_by_token(token)


@app.get("/device/guest/people")
async def device_guest_people_route(request: Request) -> dict:
    """Consent-respecting guest directory: only joined/fun-fact info guests allowed us to share."""
    dev = _device_from_request(request)
    if dev.get("profile") != "trusted_guest":
        raise HTTPException(status_code=403, detail="guest people are visible to guest devices only")
    from .guest_invites import guest_public_directory
    return {"ok": True, "people": guest_public_directory()}


@app.post("/device/guest/bug")
async def device_guest_bug_route(request: Request) -> dict:
    """A guest reports a problem (broken light, wifi dropping, tablet stuck). It becomes a bug the owner
    sees in Approvals, and the owner's devices get a push. Guest-safe: reveals nothing about the owner,
    and the guest's identity is recorded only as their device-scoped id."""
    dev = _device_from_request(request)
    is_guest = (dev.get("profile") == "trusted_guest")
    body = await request.json()
    text = str(body.get("text") or body.get("detail") or body.get("summary") or "").strip()
    if len(text) < 3:
        return {"ok": False, "error": "tell me what's not working, e.g. 'the guest room light won't turn off'"}
    from .services.bug_workflow import log_bug
    from .services import mailbox as mb
    reporter = f"guest:{dev['device_id']}" if is_guest else mb.OWNER
    res = log_bug(text, actor=reporter)
    bug_id = res.get("bug_id", "")
    if bug_id:
        label = mb._display_name(reporter) if is_guest else "you"
        await _notify_owner_of_bug(bug_id, res.get("summary", text), label)
    return {"ok": True, "bug_id": bug_id, "summary": res.get("summary", text),
            "message": f"Thanks - logged as {bug_id}. The owner will see it in Approvals."}


_PAIR_FAILS: dict[str, list[float]] = {}
_PAIR_FAIL_WINDOW_SECONDS = 300
_PAIR_FAIL_LIMIT = 8


def _pair_client_key(request: Request) -> str:
    try:
        return request.client.host if request.client else "unknown"
    except Exception:
        return "unknown"


def _pair_too_many_failures(key: str) -> bool:
    import time
    now = time.time()
    xs = [t for t in _PAIR_FAILS.get(key, []) if now - t < _PAIR_FAIL_WINDOW_SECONDS]
    _PAIR_FAILS[key] = xs
    return len(xs) >= _PAIR_FAIL_LIMIT


def _pair_record_failure(key: str) -> None:
    import time
    xs = [t for t in _PAIR_FAILS.get(key, []) if time.time() - t < _PAIR_FAIL_WINDOW_SECONDS]
    xs.append(time.time())
    _PAIR_FAILS[key] = xs[-_PAIR_FAIL_LIMIT:]


def _pair_clear_failures(key: str) -> None:
    _PAIR_FAILS.pop(key, None)

@app.post("/pair/claim")
async def pair_claim(request: Request) -> dict:
    """A device redeems a pairing code and registers itself, receiving a device token.
    PUBLIC (the device is not yet authenticated), but safe: it requires a valid,
    unexpired, single-use code that only an authenticated user could have minted."""
    from .devices import claim_pairing_code
    key = _pair_client_key(request)
    if _pair_too_many_failures(key):
        return JSONResponse(status_code=429, content={"ok": False, "error": "too many invalid pairing attempts; wait a few minutes"})
    try:
        payload = await request.json()
    except Exception:
        _pair_record_failure(key)
        return {"ok": False, "error": "invalid request body"}
    result = claim_pairing_code(
        (payload.get("code") or "").strip(),
        (payload.get("device_name") or "").strip(),
        (payload.get("device_type") or "android").strip(),
        (payload.get("display_name") or payload.get("person_name") or "").strip())
    if result.get("ok"):
        _pair_clear_failures(key)
        # The home-screen entry must be the APP (launchpad), reachable so the very next tap
        # after pairing works, AND durable, since whatever gets Added to Home Screen becomes
        # a PERMANENT bookmark for that icon. A LAN address only ever works on this one home
        # network; the DDNS address is the only one that works both here and away. So the
        # bookmark target MUST be DDNS when DDNS is configured, never LAN, even though LAN is
        # the guaranteed-reachable choice for the pairing flow itself moments earlier.
        #
        # CORRECTED (this previously flipped to LAN-first here, treating "avoid the DDNS
        # failure" as the fix; that broke the actual requirement instead of fixing the actual
        # problem). The real problem is that DDNS itself may not be reachable RIGHT NOW from
        # THIS network, most commonly because the router doesn't support NAT loopback/hairpin
        # back to its own public hostname, which specifically affects a phone on the SAME LAN
        # that just paired, not phones reaching Mongoose from elsewhere. Mongoose cannot fix a
        # router's hairpin support itself, so instead of hiding the DDNS address (breaking
        # away-from-home use) or silently sending the phone to a route diagnosed as likely to
        # fail, this now runs a real reachability check and tells the pairing page the honest
        # result, so the UI can explain what's happening and offer both the durable bookmark
        # and a temporary LAN link for right now.
        try:
            scheme = "https" if request.url.scheme == "https" else "http"
            lan_base = f"{scheme}://{_phone_reachable_host(request)}"
            ddns_base = _guest_reachable_url(request)
            has_ddns = bool(ddns_base) and ddns_base != lan_base
            if has_ddns:
                result["launchpad_url"] = ddns_base + "/device/launchpad"
                result["launchpad_is_remote"] = True
                result["launchpad_lan_fallback_url"] = lan_base + "/device/launchpad"
                try:
                    from .ddns import reachability_diagnosis
                    from .setup_wizard import effective_env
                    result["ddns_reachability"] = reachability_diagnosis(effective_env())
                except Exception:
                    result["ddns_reachability"] = {"ok": None, "reason": "probe_error"}
            else:
                result["launchpad_url"] = lan_base + "/device/launchpad"
                result["launchpad_is_remote"] = False
        except Exception:
            result["launchpad_url"] = "/device/launchpad"
            result["launchpad_is_remote"] = False
    else:
        _pair_record_failure(key)
    return result




@app.get("/device/launchpad", response_class=HTMLResponse)
def device_launchpad() -> HTMLResponse:
    """Public static PWA shell for an already-paired phone/tablet. It reads the
    device token from localStorage and calls /device/*; private data is not embedded
    in the HTML response."""
    from .cockpit import device_launchpad_html
    return _launchpad_response(device_launchpad_html())


@app.get("/ui-asset/{name}")
def ui_asset_route(name: str):
    """Serve externalised cockpit CSS/JS: content-hashed and immutable-cached, so the heavy UI code
    downloads once and is reused across loads (the HTML doc stays small + no-store). Public and
    data-free: this is just the UI code, the same class as /static. The hash in the filename is the
    cache key; content is registered when the corresponding page renders (and pre-registered at import)."""
    from fastapi.responses import Response
    from .cockpit import UI_ASSETS
    entry = UI_ASSETS.get(name.rsplit(".", 1)[0])
    if not entry:
        return Response(status_code=404)
    body, ctype = entry
    return Response(content=body, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


def _launchpad_response(html: str):
    # no-store so a phone never runs a stale shell after an update: without it the browser's HTTP
    # cache can serve an old copy even though the service worker is network-first, which is exactly
    # how a PWA got stuck on pre-update UI (missing poke dropdowns, old tabs) across deploys. The heavy
    # CSS/JS is now externalised to immutable-cached /ui-asset files, so this small HTML staying
    # no-store no longer means re-downloading the whole ~200KB payload each visit.
    return HTMLResponse(content=html,
                        headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/device/cockpit", response_class=HTMLResponse)
def device_phone_cockpit() -> str:
    """Phone-native, full-Cockpit experience for the owner's paired phone. Static HTML; its
    /cockpit/* calls authenticate with the device token (owner = full surface, guest refused)."""
    from .cockpit import phone_cockpit_html
    return phone_cockpit_html()


@app.get("/pair", response_class=HTMLResponse)
def pair_page(code: str = "") -> str:
    """A small PWA pairing landing page the phone opens after scanning the QR (or by
    typing the host). It reads the code from the URL and claims it. PUBLIC.

    If the code in the URL is already used or expired, render a clear 'link used/expired'
    page instead of the naming form, so a dead link never looks like a working pairing."""
    from .cockpit import pair_html
    if code:
        try:
            from .devices import pairing_code_status
            st = pairing_code_status(code)
            if st.get("found") and (st.get("consumed") or st.get("expired")):
                why = "already been used" if st.get("consumed") else "expired"
                return (
                    '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<title>Mongoose pairing</title>'
                    '<link rel="icon" type="image/png" sizes="32x32" href="/static/img/mongoose-32.png">'
                    '<style>html,body{margin:0;height:100%;background:#16140F;color:#ECE6DA;'
                    'font:16px/1.5 system-ui,sans-serif;display:flex;align-items:center;'
                    'justify-content:center;text-align:center}div{max-width:420px;padding:28px}'
                    'h1{font-size:20px;margin:14px 0 6px}p{color:#A69D8E}img{width:84px;height:84px}'
                    '</style></head><body><div>'
                    '<img src="/static/img/mongoose-256.png" alt="Mongoose">'
                    '<h1>This pairing link has ' + why + '</h1>'
                    '<p>Each pairing link works once and only for a few minutes. Ask the owner for a '
                    'fresh link and open that instead. If your device is already paired, just open '
                    'Mongoose normally.</p></div></body></html>'
                )
        except Exception:
            pass
    return pair_html()


@app.post("/cockpit/wizard/save")
async def cockpit_wizard_save(request: Request) -> dict:
    """Save one integration's fields (writes .env via the same path the CLI uses) and
    verify it. Nonce-gated (it's a privileged write)."""
    _require_cockpit_nonce(request)
    from .wizard import save_integration
    payload = await request.json()
    slug = payload.get("slug", "")
    values = payload.get("values", {}) or {}
    if not isinstance(values, dict):
        return {"ok": False, "error": "values must be an object"}
    return save_integration(slug, values)


@app.post("/cockpit/wizard/verify")
async def cockpit_wizard_verify(request: Request) -> dict:
    """Run an integration's verifier against current config (no save). Nonce-gated."""
    _require_cockpit_nonce(request)
    from .wizard import verify_integration
    payload = await request.json()
    return verify_integration(payload.get("slug", ""))


@app.post("/cockpit/wizard/authorize/{provider}")
async def cockpit_wizard_authorize(provider: str, request: Request) -> dict:
    """Run the OAuth sign-in for Google or Microsoft from the Cockpit setup panel, so the
    'Sign in' button is actually clickable instead of inert command text. Nonce-gated (privileged:
    it opens a consent flow on the Mac / starts a device-code flow). The flow itself runs on the
    machine hosting Mongoose; Google opens the system browser, Microsoft prints/returns a device
    code. Runs in a worker thread so the consent wait never blocks the event loop."""
    _require_cockpit_nonce(request)
    if provider not in ("google", "microsoft"):
        return {"ok": False, "error": "provider must be google or microsoft"}
    try:
        import asyncio as _asyncio
        from .oauth import authorize_google, authorize_microsoft
        fn = authorize_google if provider == "google" else authorize_microsoft
        ok, msg = await _asyncio.to_thread(fn)
        wired = []
        if ok:
            # Self-wiring: activate the unlocked capabilities now (first sync) so the cockpit can
            # show them coming online immediately, not after the next scheduled sync.
            try:
                from .services.self_wiring import wire_after_authorize
                w = await _asyncio.to_thread(wire_after_authorize, provider)
                wired = w.get("wired", [])
            except Exception:
                pass
        return {"ok": bool(ok), "message": msg, "wired": wired}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"sign-in could not start: {exc}"}


@app.get("/cockpit/routines")
def cockpit_routines_list(request: Request) -> dict:
    """List saved routines. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services.routines import list_routines
    return {"ok": True, "routines": list_routines()}


@app.post("/cockpit/routines/save")
async def cockpit_routines_save(request: Request) -> dict:
    """Create or update a routine. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services.routines import save_routine
    body = await request.json()
    return save_routine(str(body.get("name") or ""), str(body.get("recipe") or ""),
                        trigger_phrase=body.get("trigger_phrase"))


@app.post("/cockpit/routines/run")
async def cockpit_routines_run(request: Request) -> dict:
    """Run a routine through the standard tool loop (all safety guards apply). Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services.routines import run_routine
    body = await request.json()
    return await run_routine(str(body.get("name") or ""))


@app.post("/cockpit/routines/delete")
async def cockpit_routines_delete(request: Request) -> dict:
    """Delete a routine. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services.routines import delete_routine
    body = await request.json()
    return delete_routine(str(body.get("name") or ""))


@app.get("/cockpit/claude-code/status")
def cockpit_claude_code_status(request: Request) -> dict:
    """Where Claude Code lives (local + Pi), its billing posture (incl. the ANTHROPIC_API_KEY trap),
    reuse guidance, and the cost-aware headline. Read-only; never invokes Claude Code."""
    _require_cockpit_nonce(request)
    from .services.claude_code import status_report
    return status_report()


@app.get("/cockpit/protocol-v3/status")
async def cockpit_protocol_v3_status() -> dict:
    """Read-only v3 capability + Pi-transport status for the setup panel. Changes nothing."""
    from .services.protocol_v3 import supports_v3_report
    return supports_v3_report()


@app.post("/cockpit/protocol-v3/save")
async def cockpit_protocol_v3_save(request: Request) -> dict:
    """Save the Pi SSH connection details (the only thing M configures pre-cutover). Does NOT enable v3
    or touch the bus, purely scaffolding so a later 'Test connection' / handshake can run."""
    _require_cockpit_nonce(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    from .setup_wizard import write_env
    updates = {}
    host = (payload.get("pi_ssh_host") or "").strip()
    if host:
        updates["PI_SSH_HOST"] = host
    if payload.get("pi_ssh_port"):
        updates["PI_SSH_PORT"] = str(payload["pi_ssh_port"]).strip()
    if (payload.get("pi_ssh_key") or "").strip():
        updates["PI_SSH_KEY"] = payload["pi_ssh_key"].strip()
    if (payload.get("pi_config_dir") or "").strip():
        updates["PI_CONFIG_DIR"] = payload["pi_config_dir"].strip()
    if not updates:
        return {"ok": False, "error": "Nothing to save, add the Pi's SSH address at least."}
    try:
        write_env(updates)
        for k, v in updates.items():
            import os as _os
            _os.environ[k] = v
        import mongoose.settings as _S
        if hasattr(_S, "_settings"):
            _S._settings = None
        if hasattr(_S.get_settings, "cache_clear"):
            _S.get_settings.cache_clear()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Could not save: {exc}"}
    return {"ok": True, "saved": sorted(updates)}


@app.post("/cockpit/protocol-v3/test-handshake")
async def cockpit_protocol_v3_test_handshake(request: Request) -> dict:
    """Run the read-only handshake connectivity test (reach Pi, see the bus, see the other agents). This
    is the SKIPPABLE 'Test connection' step, it writes nothing and going live is never required to use
    Mongoose. Runs off-thread so the SSH round-trip can't block the event loop."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.protocol_v3 import handshake_test
    return await _asyncio.to_thread(handshake_test)


@app.post("/cockpit/protocol-v3/setup-ssh")
async def cockpit_protocol_v3_setup_ssh(request: Request) -> dict:
    """Delightful one-call Pi SSH setup: generate mongoose's dedicated key if needed, authorise it on
    the Pi (ssh-copy-id, optionally with the supplied password via sshpass for a fully-automated run),
    verify passwordless access, and, if v3 is on, greet peers immediately. Runs off-thread. The
    password is used once and never stored."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    host = (payload.get("pi_ssh_host") or "").strip() or None
    port = payload.get("pi_ssh_port")
    password = payload.get("password") or None
    from .services.protocol_v3 import setup_pi_ssh
    return await _asyncio.to_thread(setup_pi_ssh, password, host=host,
                                    port=int(port) if port else None)


@app.post("/cockpit/protocol-v3/setup-ssh-password")
async def cockpit_protocol_v3_setup_ssh_password(request: Request) -> dict:
    """Keyless Pi SSH setup: connect with just the address + password (no keys, no ssh-copy-id). Saves
    the credentials, verifies the connection, and, if v3 is on, greets peers. The password is stored
    in .env (plaintext, like the IMAP password) and needs sshpass on the Mac; the response says so
    honestly. Off-thread + nonce-gated."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    host = (payload.get("pi_ssh_host") or "").strip() or None
    port = payload.get("pi_ssh_port")
    password = payload.get("password") or ""
    from .services.protocol_v3 import setup_pi_ssh_password
    return await _asyncio.to_thread(setup_pi_ssh_password, password, host=host,
                                    port=int(port) if port else None)


@app.post("/cockpit/protocol-v3/handshake-now")
async def cockpit_protocol_v3_handshake_now(request: Request) -> dict:
    """Manually trigger the auto-handshake (force=True). Normally mongoose posts its handshake by itself
    the moment the link opens; this is for re-greeting on demand. Gated: does nothing unless v3 is on."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.protocol_v3 import post_handshake_when_open
    return await _asyncio.to_thread(post_handshake_when_open, force=True)


@app.get("/cockpit/protocol-v3/bus")
async def cockpit_protocol_v3_bus() -> dict:
    """Read the shared bus tail (messages addressed to mongoose since our last processed one). Read-only,
    off-thread, M-gated by protocol_v3."""
    import asyncio as _asyncio
    from .services.protocol_v3_runtime import read_bus
    return await _asyncio.to_thread(read_bus)


@app.get("/cockpit/protocol-v3/voicemail")
async def cockpit_protocol_v3_voicemail() -> dict:
    """Peek at the RTM's voicemails waiting on the bus staging file (does NOT ack from this read-only
    glance). Off-thread, M-gated."""
    import asyncio as _asyncio
    from .services.protocol_v3_runtime import drain_voicemail
    return await _asyncio.to_thread(lambda: drain_voicemail(ack=False))


@app.post("/cockpit/protocol-v3/reconcile")
async def cockpit_protocol_v3_reconcile(request: Request) -> dict:
    """Run the wake reconcile now: catch up on bus + voicemail backlog, ack what's addressed to us,
    protocol-version check, post a heartbeat. Takes NO household action (M-approval-gated). Nonce-gated
    because it posts acks/heartbeats."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.protocol_v3_runtime import reconcile_on_wake
    return await _asyncio.to_thread(reconcile_on_wake)


@app.get("/cockpit/device-trust/status")
async def cockpit_device_trust_status() -> dict:
    """Where HTTPS/device-trust stands (cert present? mkcert available? what method?). Pure read."""
    from .device_trust import trust_status
    return trust_status()


@app.post("/cockpit/device-trust/setup")
async def cockpit_device_trust_setup(request: Request) -> dict:
    """Generate the HTTPS cert (trusted CA via mkcert when available, else self-signed) and return the
    device-trust plan: a QR-led, per-platform guide to the one unavoidable 'trust this CA' tap. Runs
    off-thread (mkcert/openssl can take a moment)."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    from .device_trust import setup_device_trust
    return await _asyncio.to_thread(setup_device_trust)


@app.get("/cockpit/device-trust/plan")
async def cockpit_device_trust_plan(platform: str = "ios") -> dict:
    """The per-platform device-trust steps (ios/android/macos) for the current cert, without
    regenerating it. Lets the UI switch platform tabs cheaply."""
    from .device_trust import device_trust_plan
    return device_trust_plan(platform)


@app.get("/trust/ca.pem")
async def trust_ca_download() -> Response:
    """Serve mongoose's CA root certificate for installing on a phone/tablet (the trusted-CA path).
    This is the file the device downloads, then trusts in Settings. Not nonce-gated: it's a public
    certificate (the CA's PUBLIC cert, never the private key), and it must be reachable by a device
    that isn't paired yet. Returns 404 in self-signed mode (no CA to install)."""
    from .device_trust import ca_file_bytes
    got = ca_file_bytes()
    if not got:
        raise HTTPException(status_code=404, detail="no CA to install (self-signed mode, or cert not "
                            "generated yet). Run device-trust setup first.")
    data, filename = got
    return Response(content=data, media_type="application/x-pem-file",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/cockpit/microsoft/signin-begin")
async def cockpit_microsoft_signin_begin(request: Request) -> dict:
    """Start a Microsoft To Do device-code sign-in and RETURN the code + URL to the browser, so the user
    sees them in the cockpit instead of hidden in the Mac terminal. Completes in the background; poll
    /cockpit/microsoft/signin-status. Works WITHOUT an app registration (well-known public client)."""
    _require_cockpit_nonce(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    todo_only = bool(payload.get("todo_only", True))
    import asyncio as _asyncio
    from .connectors.microsoft_graph import begin_device_signin
    return await _asyncio.to_thread(begin_device_signin, todo_only)


@app.get("/cockpit/microsoft/signin-status")
async def cockpit_microsoft_signin_status(request: Request) -> dict:
    """Poll the background Microsoft device-code sign-in."""
    from .connectors.microsoft_graph import device_signin_status
    return device_signin_status()


@app.post("/cockpit/wizard/upload-google-json")
async def cockpit_wizard_upload_google_json(request: Request) -> dict:
    """Accept the Google OAuth client JSON as an upload (the browser posts the file's text as JSON),
    validate its shape, save it to the configured secrets path, and record that path in .env. This
    replaces asking the user to type a filesystem path. Saving the JSON is scaffolding only, it does
    NOT mark Google as signed in; the user still clicks 'Sign in with Google'. Nonce-gated."""
    _require_cockpit_nonce(request)
    import json as _json
    from pathlib import Path as _Path
    payload = await request.json()
    raw = payload.get("content", "")
    if not raw or not isinstance(raw, str):
        return {"ok": False, "error": "no file content received"}
    try:
        parsed = _json.loads(raw)
    except Exception:
        return {"ok": False, "error": "that file isn't valid JSON, upload the OAuth client JSON you downloaded from Google Cloud Console"}
    # Google OAuth client files have a top-level "installed" or "web" object with a client_id.
    holder = parsed.get("installed") or parsed.get("web") or {}
    if not isinstance(holder, dict) or not holder.get("client_id"):
        return {"ok": False, "error": "this JSON doesn't look like a Google OAuth client file (no installed/web client_id). Make sure you created an OAuth client ID of type 'Desktop app' and downloaded its JSON."}
    from .settings import get_settings
    dest = _Path(get_settings().google_client_secrets_path)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(raw, encoding="utf-8")
        try:
            dest.chmod(0o600)
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"couldn't save the JSON: {exc}"}
    from .wizard import save_integration
    save_integration("google_calendar", {"GOOGLE_CLIENT_SECRETS_PATH": str(dest)})
    from .oauth import google_oauth_redirect_uri
    return {"ok": True,
            "message": (f"Saved OAuth client JSON to {dest}. IMPORTANT: in the Google Cloud Console, on this "
                        f"OAuth client, add this Authorized redirect URI: {google_oauth_redirect_uri()} "
                        f"(exactly, including the trailing slash). Then click 'Sign in with Google'."),
            "redirect_uri": google_oauth_redirect_uri(),
            "path": str(dest), "next": "sign_in"}


@app.get("/cockpit/mail")
def cockpit_mail() -> dict:
    """Per-provider mail sync status for the cockpit (authorised + freshness)."""
    from .mail_status import mail_status
    return mail_status()


@app.post("/cockpit/mail/sync")
async def cockpit_mail_sync(request: Request) -> dict:
    """Run one mail sync cycle now and report. Nonce-gated (it triggers real work)."""
    _require_cockpit_nonce(request)
    from .mail_status import sync_now
    return await sync_now()


def _log_files() -> dict:
    """The set of viewable log files, keyed by base name. ONLY files directly inside
    the resolved log dir are included, this enumeration is the traversal guard: the
    viewer can only ever open a name that appears here, never an arbitrary path."""
    from .service import _log_dir
    out = {}
    try:
        d = _log_dir()
        for f in sorted(d.glob("*.log")):
            if f.is_file():
                out[f.name] = f
    except Exception:
        pass
    return out


@app.get("/cockpit/logs")
def cockpit_logs() -> dict:
    """List viewable log files with sizes. The Logs tab lists these; each is opened by
    its base name via /cockpit/logs/{name}."""
    from .service import _log_dir
    files = _log_files()
    items = []
    for name, path in files.items():
        try:
            size = path.stat().st_size
        except Exception:
            size = 0
        items.append({"name": name, "bytes": size})
    return {"logs": items, "dir": str(_log_dir())}


@app.get("/cockpit/logs/{name}")
def cockpit_log_tail(name: str, kb: int = 64) -> dict:
    """Return the TAIL of one named log file (last `kb` kilobytes, capped). The name is
    validated against the enumerated set (no path traversal: an unknown name 404s), and
    the read is bounded (no shipping a huge file into memory/the browser)."""
    files = _log_files()
    path = files.get(name)
    if path is None:
        raise HTTPException(status_code=404, detail="unknown log file")
    kb = max(1, min(kb, 1024))  # 1KB..1MB
    want = kb * 1024
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > want:
                fh.seek(size - want)
                truncated = True
            else:
                truncated = False
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
        if truncated:
            # drop a likely-partial first line
            nl = text.find("\n")
            if nl != -1:
                text = text[nl + 1:]
        return {"name": name, "bytes": size, "truncated": truncated, "text": text}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not read log: {exc}")


@app.get("/release-check")
async def release_check_endpoint() -> dict:
    """Runtime readiness verdict + per-line states, for the CLI and the cockpit."""
    from .release_check import release_check
    return await _status_cached_async("release_check", 120.0, release_check)


@app.get("/alpha-check")
async def alpha_check_endpoint_legacy() -> dict:
    """Back-compat alias for /release-check. Older clients and any cached cockpit JS may still
    call /alpha-check; route them to the same readiness payload so nothing breaks on upgrade."""
    from .release_check import release_check
    # Same SWR cache as /release-check , the legacy alias must not re-run the full doctor + model-residency
    # probe on every hit (it was bypassing the cache and paying the cold cost each time).
    return await _status_cached_async("release_check", 120.0, release_check)


@app.get("/cockpit/daily")
def cockpit_daily() -> dict:
    """The Daily Operating Picture: the cross-domain shape of the day, synthesised across every domain.

    SYNC endpoint on purpose: FastAPI runs sync handlers in the threadpool, so even a cold model
    synthesis runs OFF the event loop and can never block new connections (an async version blocked the
    loop for ~30s and dropped the Cloudflare tunnel origin, which timed out unrelated screens like
    Activity). SWR-cached and prewarmed at boot, so interactive polls serve the last picture instantly."""
    import asyncio as _aio
    from .services.daily_picture import daily_operating_picture
    return _status_cached("daily", 180.0, lambda: _aio.run(daily_operating_picture()))




@app.get("/cockpit/prepare")
async def cockpit_prepare(q: str = Query("today")) -> dict:
    """Prepare-me card for an event/person/project/task/place/situation, grounded in
    local calendar/email/files/WhatsApp/memory where available."""
    from .services.prepare import prepare_card
    return await prepare_card(q)


@app.get("/cockpit/suggest")
async def cockpit_suggest() -> dict:
    """On-demand: ask Mongoose for a useful unprompted suggestion now (the surpriser).
    Read-only and idempotent-ish (novelty still applies). Has a short timeout so a slow
    or busy model can't freeze the overview card."""
    import asyncio as _aio
    from .services.surpriser import pick_surprise
    try:
        return await _aio.wait_for(pick_surprise(force=True, source="on_demand"), timeout=25.0)
    except _aio.TimeoutError:
        return {"ok": False, "suggestion": None,
                "note": "suggestion timed out (model may be busy), try again in a moment"}


@app.get("/cockpit/suggestions")
def cockpit_suggestions(request: Request) -> dict:
    """Recent surfaced suggestions and their feedback (for the cockpit). Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services.surpriser import recent_suggestions
    return {"ok": True, "suggestions": recent_suggestions(20)}


@app.post("/cockpit/suggestions/feedback")
async def cockpit_suggestions_feedback(request: Request) -> dict:
    """Record thumbs up/down on a surfaced suggestion, so Mongoose learns what lands. Nonce-gated."""
    _require_cockpit_nonce(request)
    try:
        p = await request.json()
    except Exception:
        return {"ok": False, "error": "JSON body required: send Content-Type: application/json with a JSON body, or the body is malformed"}
    sig = str(p.get("signature") or "")
    fb = str(p.get("feedback") or "")
    if not sig or fb not in ("useful", "not_useful", "dismissed"):
        return {"ok": False, "error": "signature and a valid feedback value (useful/not_useful/dismissed) are required"}
    from .services.surpriser import record_feedback
    return record_feedback(sig, fb)


@app.get("/cockpit/receipts")
def cockpit_receipts(source: str = "", limit: int = 50) -> dict:
    """The unified receipts ledger: one honest answer to 'what did Mongoose do?',
    composed from device, surpriser, scheduler, and WhatsApp-draft sources."""
    from .services.receipts import receipts_ledger
    return receipts_ledger(limit=limit, source=(source or None))


@app.post("/cockpit/suggest/feedback")
async def cockpit_suggest_feedback(request: Request) -> dict:
    """Record useful / not_useful on a suggestion. Nonce-gated (it's a write)."""
    _require_cockpit_nonce(request)
    from .services.surpriser import record_feedback
    payload = await request.json()
    return record_feedback(payload.get("signature", ""), payload.get("feedback", ""))


@app.post("/cockpit/chat")
async def cockpit_chat(request: Request) -> dict:
    """Text chat: a typed message in, Mongoose's reply out. Same brain as voice (it runs through
    chat_with_memory, so it gets the full routing, delight, RTM, body, readiness, everything) but
    with no transcription step. Requires the cockpit nonce (it runs a real turn). Keeps its own
    conversation thread so the text chat has continuity across messages."""
    _require_cockpit_nonce(request)
    # Server-side stale-session enforcement (the epoch system used to be advisory only: a stale
    # tab was asked, client-side, to notice it had been superseded, but the server never refused
    # a write, so a second browser could interleave writes into the ONE shared conversation
    # indefinitely). A cockpit tab sends the boot_id:epoch it claimed; if a newer tab has claimed
    # since (higher epoch) or the server restarted (different boot id), this write is refused
    # with a clear signal instead of silently landing mid-conversation. Callers that don't send
    # the header (phone /device/chat has its own route; token scripts) are unchanged.
    claimed = (request.headers.get("x-cockpit-epoch") or "").strip()
    if claimed:
        boot, _, ep = claimed.partition(":")
        try:
            ep_n = int(ep or "-1")
        except ValueError:
            ep_n = -1
        if boot != _SESSION_BOOT_ID or ep_n < _SESSION_EPOCH:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=409, content={
                "ok": False, "error": "stale_session",
                "detail": ("This tab was superseded by a newer Mongoose tab (or the server "
                           "restarted) after it loaded. Nothing was lost; reload this page to "
                           "continue the conversation from where it really is.")})
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "error": "expected a JSON body"}
    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "no message"}
    if len(text) > 8000:
        return {"ok": False, "error": "message too long (max 8000 chars)"}
    from .llm import chat_with_memory
    from .services.memory import DEFAULT_CONVERSATION_ID
    # Owner mailbox hook: "tell Jo X" / "message everyone Y" / "read my messages" handled here too,
    # so the owner can reach guests from the Mac cockpit, not just the phone.
    _owner_dev = {"device_id": "owner", "profile": "owner"}
    mb_reply = _maybe_handle_message_send(text, is_guest=False, dev=_owner_dev)
    if mb_reply is not None:
        return {"ok": True, "reply": mb_reply, "source": "mailbox", "remembered": [], "memory_events": []}
    # Routine trigger: if the message names a saved routine (exact name, "run X", or a trigger
    # phrase), execute it through the SAME tool loop a typed request uses: so every safety guard
    # (injection, sensitive-action confirmation, panel safety) still applies. Conservative match.
    try:
        from .services import routines as _rt
        matched = _rt.match_routine(text)
        if matched:
            rr = await _rt.run_routine(matched["name"])
            reply = rr.get("reply") or "Done."
            return {"ok": True, "reply": reply, "source": "routine",
                    "routine": matched["name"], "steps": rr.get("steps", []),
                    "remembered": [], "memory_events": []}
    except Exception:
        pass  # never let routine matching break normal chat
    result = await chat_with_memory(text, conversation_id=DEFAULT_CONVERSATION_ID, role="chat")
    reply = result.get("reply") or result.get("response") or result.get("text") or ""
    # Surface unread messages to the owner too.
    try:
        from .services import mailbox as _mb
        n = _mb.unread_count(_mb.OWNER)
        if n:
            reply = f"{reply}\n\n(You have {n} new message{'s' if n != 1 else ''} from guests: say \"read my messages\".)"
    except Exception:
        pass
    # Occasionally ask for training/nutrition data so the body features stay real. The nudge paces
    # itself (it only fires when data is missing/stale and not too soon after the last ask), so this
    # is rare, not a nag. Importing happens via the Body data card or a phone upload.
    try:
        from .services import body_data_nudge as _bdn
        _nudge = _bdn.due_nudge()
        if _nudge:
            reply = f"{reply}\n\n{_nudge['ask']}"
            _bdn.mark_asked(_nudge["reason"])
    except Exception:
        pass
    return {"ok": True, "reply": reply, "source": result.get("source", ""),
            "remembered": result.get("remembered", []),
            "memory_events": result.get("memory_events", [])}


@app.get("/cockpit/ddns/options")
def cockpit_ddns_options() -> dict:
    """Everything the browser needs to render the remote-reach chooser (providers, categories,
    trade-offs, fields) plus current state."""
    from .ddns import options_payload
    return options_payload()


@app.post("/cockpit/ddns/save")
async def cockpit_ddns_save(request: Request) -> dict:
    """Save a chosen DDNS provider + its fields from the browser, and do a first update for feedback."""
    _require_cockpit_nonce(request)
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "detail": "expected a JSON body"}
    provider = (payload.get("provider") or "").strip()
    values = payload.get("values") or {}
    if not provider:
        return {"ok": False, "detail": "no provider chosen"}
    from .ddns import save_ddns
    import asyncio as _aio
    # save_ddns does a blocking urllib call (DNS provider update). On an async route that blocks the WHOLE
    # event loop, every other request freezes until it returns or times out. Run it off-thread.
    return await _aio.to_thread(save_ddns, provider, values, True)


@app.post("/cockpit/ddns/update")
async def cockpit_ddns_update(request: Request) -> dict:
    """Trigger an IP update now from the browser."""
    _require_cockpit_nonce(request)
    from .ddns import update_now
    import asyncio as _aio
    return await _aio.to_thread(update_now, None, True)


@app.post("/cockpit/ddns/disable")
async def cockpit_ddns_disable(request: Request) -> dict:
    """Turn auto-update off (keeps credentials)."""
    _require_cockpit_nonce(request)
    from .ddns import disable_ddns
    return disable_ddns()


@app.post("/cockpit/ddns/tailscale-up")
async def cockpit_ddns_tailscale_up(request: Request) -> dict:
    """Best-effort Tailscale bring-up from the browser. This can only work on the Mac itself and may
    need sudo/sign-in, so it reports honestly whether it could run rather than pretending."""
    _require_cockpit_nonce(request)
    import shutil
    import platform
    if platform.system() != "Darwin":
        return {"ok": False, "detail": "Tailscale setup runs on the Mac. On this OS, install from tailscale.com/download then run `tailscale up`."}
    if not shutil.which("tailscale") and not shutil.which("brew"):
        return {"ok": False, "detail": "Neither Tailscale nor Homebrew is installed. Install Homebrew (brew.sh), then run `mongoose ddns setup` in a terminal."}
    # `tailscale up` opens an interactive browser sign-in and may prompt for sudo; that can't be
    # driven from a web request. Tell the user to run the one terminal command, honestly.
    return {"ok": False, "needs_terminal": True,
            "detail": "Tailscale needs a one-time sign-in (and sudo) that can't run from this page. In a terminal run:  mongoose ddns setup --provider tailscale, it installs Tailscale and signs you in. This page will then show it as active."}


@app.get("/cockpit/selfcheck")
def cockpit_selfcheck() -> dict:
    """Tiny diagnostic: reports the running server's version and granular checks on whether THIS
    server's cockpit HTML actually has the current chat UI (present AND reachable, not trapped inside
    a media query), so the recurring 'I see old/plain chat UI' question can be settled with data
    instead of guessing stale-server vs real bug. Open /cockpit/selfcheck in the browser."""
    from .cockpit import cockpit_html
    from . import __version__
    h = cockpit_html()
    # chat lime CSS is only truly active if the rule exists AND isn't trapped inside the
    # prefers-reduced-motion block (that trap made the terminal styling invisible to most users).
    lime_present = "#39ff14" in h and ".irc-win{" in h
    trapped = False
    try:
        reduced = h.index("@media (prefers-reduced-motion: reduce)")
        chat = h.index("/* ===== Chat tab")
        # the chat CSS must come AFTER a closed reduced-motion block, not inside it
        trapped = not (chat > reduced and "}\n  /* ===== Chat tab" in h[reduced:chat + 120])
        # if the chat marker appears before any reduced-motion block, it's also fine (not trapped)
        if chat < reduced:
            trapped = False
    except ValueError:
        trapped = False
    return {
        "version": __version__,
        "chat_tab_present": '<section class="tab" id="chat">' in h or 'id="chat"' in h,
        "chat_lime_css_present": lime_present and not trapped,
        "chat_css_trapped_in_reduced_motion": trapped,
        "auto_reload_excludes_chat": ('active.id!=="chat"' in h or 'active.id !== "chat"' in h
                                      or ('NO_AUTO_REFRESH' in h and '"chat"' in h)),
        "chat_unified_conversation": True,
        "hint": "If chat_lime_css_present is true but your browser shows plain chat UI, hard-refresh "
                "(Cmd+Shift+R), it's browser cache. If version is older than expected, an old "
                "server is running; restart Mongoose. If chat_css_trapped_in_reduced_motion is true, "
                "that's a real bug to report.",
    }


@app.post("/cockpit/reindex-files")
async def cockpit_reindex_files(request: Request) -> dict:
    """Index the configured folders, triggered from the browser (so 'run mongoose reindex-files' in a
    terminal isn't required). Indexes each path in MONGOOSE_INDEX_ROOTS. Returns immediately and runs
    in the background so the browser doesn't block waiting for a potentially long index."""
    _require_cockpit_nonce(request)
    import os as _os
    import asyncio as _aio
    roots = (_os.getenv("MONGOOSE_INDEX_ROOTS", "") or "").strip()
    if not roots:
        return {"ok": False, "error": "No folders are set to index yet. Add them in Setup, "
                "Background tasks (Folders to index), then reindex."}

    async def _do_reindex():
        from .services.file_index import index_files
        for spec in [r.strip() for r in roots.split(",") if r.strip()]:
            try:
                path = spec.split(":")[0] if ":" in spec and "/" not in spec.split(":")[0] else spec
                device = spec.split(":", 1)[1] if ":" in spec and "/" not in spec.split(":")[0] else "mac"
                await _aio.to_thread(index_files, path, device)
            except Exception:
                pass

    _aio.create_task(_do_reindex())
    return {"ok": True, "message": "Indexing started in the background. Check the Search tab in a moment."}
@app.get("/cockpit/chat/history")
def cockpit_chat_history(request: Request) -> dict:
    """Recent messages from the single continuous conversation, so the chat tab shows history on
    open (voice and text share 'mongoose-main', so this reflects both)."""
    _require_cockpit_nonce(request)
    try:
        from .db import connect
        from .services.memory import DEFAULT_CONVERSATION_ID
        # Query directly (not recent_messages, which drops the id) so the payload carries the monotonic
        # id + created_at: the authoritative order/dedupe key, consistent with the phone history payload.
        with connect() as con:
            rows = con.execute(
                "SELECT id, created_at, role, content FROM messages WHERE conversation_id = ? "
                "ORDER BY id DESC LIMIT ?", (DEFAULT_CONVERSATION_ID, 40)).fetchall()
        msgs = [{"id": r["id"], "created_at": r["created_at"], "role": r["role"],
                 "content": r["content"] or ""} for r in reversed(rows)]
        return {"ok": True, "messages": msgs}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "messages": [], "note": str(exc)}


@app.post("/cockpit/voice")
async def cockpit_voice(request: Request) -> dict:
    """Voice relay: a browser records a clip and POSTs the audio bytes here; the
    server transcribes with the existing Whisper path and runs the transcript through
    the conversation OWNER (the single speaker), returning {transcript, reply}. The
    browser speaks the reply with its own speech synthesis (no server TTS). SKIPs
    clearly if Whisper isn't installed. Requires the cockpit nonce (it runs a turn)."""
    import tempfile
    from pathlib import Path
    _require_cockpit_nonce(request)
    # Same stale-session gate as /cockpit/chat: a voice turn writes into the SAME shared
    # conversation, so a superseded tab's microphone must be refused for exactly the same
    # reason its keyboard is.
    claimed = (request.headers.get("x-cockpit-epoch") or "").strip()
    if claimed:
        boot, _, ep = claimed.partition(":")
        try:
            ep_n = int(ep or "-1")
        except ValueError:
            ep_n = -1
        if boot != _SESSION_BOOT_ID or ep_n < _SESSION_EPOCH:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=409, content={
                "ok": False, "error": "stale_session",
                "detail": ("This tab was superseded by a newer Mongoose tab (or the server "
                           "restarted) after it loaded. Nothing was lost; reload this page to "
                           "continue the conversation from where it really is.")})
    # Cap the upload: a logged-in browser shouldn't be able to push a huge blob into
    # memory/disk. Reject by Content-Length first (cheap), then bound the read.
    _MAX_AUDIO = 25 * 1024 * 1024  # 25 MB, generous for a short spoken clip
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > _MAX_AUDIO:
        return {"ok": False, "error": "audio too large (max 25MB)"}
    body = await request.body()
    if not body:
        return {"ok": False, "error": "no audio uploaded"}
    if len(body) > _MAX_AUDIO:
        return {"ok": False, "error": "audio too large (max 25MB)"}
    import asyncio as _asyncio
    import os as _os
    from .voice_benchmark import _whisper_transcribe
    # Browser audio MIME varies (Chrome gives webm/opus, Safari gives mp4/aac). Pick the file
    # extension from what the browser actually declared so Whisper gets a correctly-named file.
    raw_mime = (request.headers.get("x-audio-mime") or "audio/webm").split(";")[0].strip().lower()
    _ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4", "audio/aac": "aac",
            "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav"}.get(raw_mime, "webm")

    def _float_env(name: str, default: float) -> float:
        try:
            return float(_os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default
    stt_timeout = _float_env("MONGOOSE_COCKPIT_VOICE_STT_TIMEOUT_S", 75.0)
    brain_timeout = _float_env("MONGOOSE_COCKPIT_VOICE_LLM_TIMEOUT_S", 25.0)
    # Whisper is synchronous/subprocess-backed: run it off the event loop, and bound both STT and
    # the model so one stuck voice turn can't freeze every other cockpit/phone request.
    with tempfile.TemporaryDirectory() as d:
        clip = Path(d) / f"clip.{_ext}"
        clip.write_bytes(body)
        try:
            stt = await _asyncio.wait_for(_asyncio.to_thread(_whisper_transcribe, clip), timeout=stt_timeout)
        except _asyncio.TimeoutError:
            return {"ok": False, "error": "Transcription took too long and was stopped. Try a shorter clip."}
    if stt is None:
        return {"ok": False, "error": "Voice transcription isn't set up yet. "
                                      "Open the Setup tab \u2192 Voice transcription \u2192 Install."}
    _, transcript = stt
    if not transcript.strip():
        return {"ok": True, "transcript": "", "reply": "(I didn't catch that.)"}
    from .llm import chat_with_memory
    from .services.memory import DEFAULT_CONVERSATION_ID
    try:
        result = await _asyncio.wait_for(
            chat_with_memory(transcript, conversation_id=DEFAULT_CONVERSATION_ID, role="chat"),
            timeout=brain_timeout)
    except _asyncio.TimeoutError:
        return {"ok": True, "transcript": transcript,
                "reply": "I heard you, but the local brain is taking longer than usual. Try again in a moment."}
    reply = result.get("reply") or result.get("response") or result.get("text") or ""
    return {"ok": True, "transcript": transcript, "reply": reply}


@app.get("/setup/install-missing")
def setup_install_missing_plan() -> dict:
    """Preview (read-only): WHAT would be installed and WHERE. No nonce needed."""
    import sys
    from .services.capabilities import detect_all
    missing = [c for c in detect_all()
               if c.get("capability") and not c.get("ready") and c.get("missing_pip")]
    return {"plan": [{"capability": c["capability"], "missing_pip": c.get("missing_pip", [])}
                     for c in missing],
            "python": sys.executable}


@app.post("/setup/install-missing")
async def setup_install_missing(request: Request) -> dict:
    """Install missing detected capabilities into the active interpreter (mutating).
    Requires the cockpit nonce (the cockpit shows the GET plan and confirms first).
    Wraps the existing capabilities layer (same as `mongoose update --install-missing`).
    """
    from .services.capabilities import detect_all, install_capability
    _require_cockpit_nonce(request)
    missing = [c for c in detect_all()
               if c.get("capability") and not c.get("ready") and c.get("missing_pip")]
    installed, failed = [], []
    for c in missing:
        res = install_capability(c["capability"])
        (installed if res.get("ok") else failed).append(c["capability"])
    detail = f"installed: {installed or 'none'}" + (f"; failed: {failed}" if failed else "")
    return {"ok": not failed, "installed": installed, "failed": failed, "detail": detail}


_LOUNGE_SAFE_SURPRISE_KINDS = {"delight"}  # only harmless general ones may reach the guest-visible lounge


@app.get("/cockpit/last-surprise")
def cockpit_last_surprise(safe: int = 0) -> dict:
    """The most recently SURFACED surpriser. A surface (e.g. the phone chat) that missed the live one
    because of the shared shape-cooldown can show this once so its formatted style is visible. Read-only.
    safe=1 restricts to harmless GENERAL kinds only (for the guest-visible lounge, so owner-private
    nudges, arrears, reminders, can never leak to guests)."""
    from .services.surpriser import recent_suggestions
    from datetime import datetime, timezone
    rows = recent_suggestions(30)
    if safe:
        rows = [r for r in rows if str(r.get("kind", "")) in _LOUNGE_SAFE_SURPRISE_KINDS]

    def _reseedable(r) -> bool:
        # NEVER re-seed a DELIGHT: delights are ephemeral (a fresh fun fact each time), so re-showing a
        # days-old one on every reload is exactly the "stale France fact keeps reappearing" bug. And
        # never re-seed anything older than an hour , a "recent" surprise from days ago is not recent.
        if str(r.get("kind", "")) == "delight":
            return False
        try:
            ts = datetime.fromisoformat(str(r.get("created_at", "")).replace("Z", ""))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - ts).total_seconds() <= 3600
        except Exception:
            return False

    rows = [r for r in rows if _reseedable(r)]
    if not rows:
        return {"ok": True, "surfaced": False}
    r = rows[0]
    # The log does not store the live "why" reason, but it keeps the suggestion KIND, so surface that as
    # the little grey-italic provenance note (e.g. "delight") the main chat shows after a line.
    why = str(r.get("kind", "") or "").replace("_", " ").strip()
    return {"ok": True, "surfaced": True,
            "suggestion": {"text": r.get("text", ""), "signature": r.get("signature", ""), "why": why}}


@app.get("/cockpit/voice/status")
def cockpit_voice_status(request: Request) -> dict:
    """Consolidated voice-stack status for the Cockpit Voice card: TTS, STT toolchain, config, the
    browser push-to-talk turn, and the hands-free roadmap. Read-only; nonce-gated."""
    _require_cockpit_nonce(request)
    from .voice import voice_status
    return voice_status()


@app.get("/setup/voice")
def setup_voice_status() -> dict:
    """Check whether the voice toolchain (Whisper + ffmpeg + sox) is installed. No nonce needed."""
    import shutil
    from .voice import which_cli as _which_cli
    whisper_ok = bool(_which_cli("whisper") or _which_cli("whisper-ctranslate2"))
    ffmpeg_ok = bool(shutil.which("ffmpeg"))
    sox_ok = bool(shutil.which("rec"))
    ready = whisper_ok and ffmpeg_ok
    missing = []
    if not sox_ok: missing.append("sox (Homebrew)")
    if not ffmpeg_ok: missing.append("ffmpeg (Homebrew)")
    if not whisper_ok: missing.append("openai-whisper (pip)")
    return {"ok": True, "ready": ready, "whisper": whisper_ok, "ffmpeg": ffmpeg_ok, "sox": sox_ok,
            "missing": missing,
            "message": ("Voice transcription is ready." if ready
                        else f"Missing: {', '.join(missing)}. Click Install to set it up.")}


@app.post("/setup/voice")
async def setup_voice_install(request: Request) -> dict:
    """Install the voice toolchain (sox + ffmpeg via Homebrew, openai-whisper via pip).
    Nonce-gated. Runs in the background and returns immediately; poll /setup/voice to see
    when it's ready. This is what the cockpit 'Install voice' button calls: no terminal needed."""
    _require_cockpit_nonce(request)
    import sys
    import asyncio
    import subprocess
    import shutil
    results = []
    brew = shutil.which("brew")
    # sox
    if not shutil.which("rec") and brew:
        try:
            r = await asyncio.to_thread(
                lambda: subprocess.run([brew, "install", "sox"], capture_output=True, timeout=120))
            results.append({"pkg": "sox", "ok": r.returncode == 0, "detail": r.stderr.decode()[-200:]})
        except Exception as exc:
            results.append({"pkg": "sox", "ok": False, "detail": str(exc)})
    # ffmpeg
    if not shutil.which("ffmpeg") and brew:
        try:
            r = await asyncio.to_thread(
                lambda: subprocess.run([brew, "install", "ffmpeg"], capture_output=True, timeout=300))
            results.append({"pkg": "ffmpeg", "ok": r.returncode == 0, "detail": r.stderr.decode()[-200:]})
        except Exception as exc:
            results.append({"pkg": "ffmpeg", "ok": False, "detail": str(exc)})
    # openai-whisper
    from .voice import which_cli as _which_cli
    if not (_which_cli("whisper") or _which_cli("whisper-ctranslate2")):
        try:
            pip_cmd = [sys.executable, "-m", "pip", "install", "--quiet", "openai-whisper"]
            r = await asyncio.to_thread(lambda: subprocess.run(pip_cmd, capture_output=True, timeout=300))
            if r.returncode != 0:
                # Retry with --break-system-packages for managed Python environments
                pip_cmd2 = pip_cmd + ["--break-system-packages"]
                r = await asyncio.to_thread(lambda: subprocess.run(pip_cmd2, capture_output=True, timeout=300))
            ok = r.returncode == 0
            # Surface a clean message: the last few lines of output, not the full pip essay
            out = (r.stdout + r.stderr).decode().strip()
            last_lines = "\n".join(out.splitlines()[-5:]) if out else ""
            results.append({"pkg": "openai-whisper", "ok": ok,
                            "detail": last_lines if not ok else "installed"})
        except Exception as exc:
            results.append({"pkg": "openai-whisper", "ok": False, "detail": str(exc)})
    ok = all(r["ok"] for r in results) if results else True
    failed = [r["pkg"] for r in results if not r["ok"]]
    return {"ok": ok, "results": results,
            "message": ("Voice toolchain installed. Refresh the page to use it." if ok
                        else f"Some packages failed to install: {', '.join(failed)}. "
                             "Check the Mac has Homebrew and an internet connection.")}



# ── Build & Deploy ──────────────────────────────────────────────────────────────────────────────

@app.get("/cockpit/pi-build/status")
async def cockpit_pi_build_status() -> dict:
    """Is the Pi a usable remote build node right now, and how would a build there be billed? Read-only,
    off-thread (it probes the Pi over SSH)."""
    import asyncio as _asyncio
    from .services import remote_build as RB
    return await _asyncio.to_thread(RB.status_report)


@app.post("/cockpit/pi-build/run")
async def cockpit_pi_build_run(request: Request) -> dict:
    """Run a one-shot build on the Pi: sync the given local repo to the Pi and run `claude -p <task>`
    there. Nonce-gated, off-thread. Returns where it ran + the output tail for the Mac to review."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    repo = (payload.get("repo") or "").strip()
    task = (payload.get("task") or "").strip()
    allow_api_key = bool(payload.get("allow_api_key"))
    if not repo or not task:
        return {"ok": False, "error": "need both 'repo' (local path) and 'task' (build instruction)"}
    from .services import remote_build as RB
    return await _asyncio.to_thread(RB.coordinated_remote_build, task, repo, allow_api_key=allow_api_key)


@app.post("/build/ingest")
async def build_ingest_endpoint(request: Request) -> dict:
    """One-shot headless build ingest: accept a zip (uploaded bytes, local path, or URL), stage it,
    run the checks, and, ONLY if every check passes, approve and apply it end to end with no human
    click. This is the 'push a new build straight into Mongoose' door for an authenticated agent (the
    Claude app, a CI step, a curl one-liner).

    SECURITY: token-only (X-Mongoose-Token == MONGOOSE_API_TOKEN). The cockpit nonce is NOT accepted,
    apply is the most destructive action and must not be reachable by browser script that happens to
    hold a nonce. If MONGOOSE_API_TOKEN is unset, this endpoint refuses (you must opt in by setting a
    token), so it can never be an unauthenticated remote-code-apply hole.

    SAFETY: apply goes through the SAME updater as the manual path, it preserves .env/data/secrets,
    snapshots the brain first, and rolls back on failure. apply_update_stage itself refuses unless the
    stage is approved AND all checks passed, so a bad build is staged+checked but never applied."""
    from .settings import get_settings as _gs
    s = _gs()
    token = (getattr(s, "api_token", "") or "").strip()
    if not token:
        raise HTTPException(status_code=403,
                            detail="build ingest is disabled: set MONGOOSE_API_TOKEN to enable "
                                   "token-authenticated build pushes")
    supplied = ""
    try:
        supplied = request.headers.get("x-mongoose-token", "")
    except Exception:
        supplied = ""
    if not (supplied and _secrets.compare_digest(supplied, token)):
        raise HTTPException(status_code=403, detail="build ingest requires a valid X-Mongoose-Token")

    import os
    import tempfile as _tmp
    from pathlib import Path as _Path
    from .updater import (stage_update_candidate, check_update_stage,
                          approve_update_stage, apply_update_stage)

    # Resolve the zip source: uploaded multipart file, JSON {source: path} / {url: ...}.
    staging_path: _Path | None = None
    cleanup = False
    ctype = ""
    try:
        ctype = request.headers.get("content-type", "")
    except Exception:
        ctype = ""
    try:
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            up = form.get("file")
            if up is None:
                return {"ok": False, "error": "multipart upload missing 'file' field"}
            data = await up.read()
            fd, tmp = _tmp.mkstemp(suffix=".zip", prefix="mongoose-ingest-")
            os.close(fd)
            _Path(tmp).write_bytes(data)
            staging_path = _Path(tmp)
            cleanup = True
        else:
            body = await request.json()
            url = (body.get("url") or "").strip()
            src = (body.get("source") or "").strip()
            if url:
                import httpx as _httpx
                async with _httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
                    resp = await client.get(url)
                if resp.status_code != 200:
                    return {"ok": False, "error": f"download failed: HTTP {resp.status_code}"}
                fd, tmp = _tmp.mkstemp(suffix=".zip", prefix="mongoose-ingest-")
                os.close(fd)
                _Path(tmp).write_bytes(resp.content)
                staging_path = _Path(tmp)
                cleanup = True
            elif src:
                staging_path = _Path(src).expanduser()
                if not staging_path.exists():
                    return {"ok": False, "error": f"source not found: {staging_path}"}
            else:
                return {"ok": False, "error": "need an uploaded 'file', or JSON 'source' (path) or 'url'"}
    except Exception as exc:
        return {"ok": False, "error": f"couldn't read the build: {exc}"}

    import asyncio as _asyncio

    def _stage_check_apply() -> dict:
        # 1. stage
        st = stage_update_candidate(str(staging_path))
        if not st.get("ok"):
            return {"ok": False, "stage": "stage", "detail": st.get("error", "staging failed"), "result": st}
        stage_id = st.get("stage_id")
        # 2. check (this runs the gate against the staged build)
        chk = check_update_stage(stage_id)
        checks = chk.get("checks", [])
        all_ok = bool(checks) and all(c.get("ok") for c in checks)
        if not all_ok:
            failed = [c.get("name", "?") for c in checks if not c.get("ok")]
            return {"ok": False, "stage": "check", "stage_id": stage_id,
                    "detail": f"staged build FAILED checks ({', '.join(failed) or 'no checks ran'}); "
                              "not applying. The build is staged for inspection but was not deployed.",
                    "checks": checks}
        # 3. approve (only because checks passed) + 4. apply
        approve_update_stage(stage_id)
        rc = apply_update_stage(stage_id, yes=True)
        if rc != 0:
            return {"ok": False, "stage": "apply", "stage_id": stage_id,
                    "detail": f"apply failed (rc={rc}); the updater backs up and can roll back. "
                              "Check the server logs.", "rc": rc}
        return {"ok": True, "stage": "applied", "stage_id": stage_id,
                "detail": "Build staged, checks passed, and applied. .env/data/secrets preserved; "
                          "a brain snapshot was taken. Restart may be needed for the new code to serve."}

    try:
        return await _asyncio.to_thread(_stage_check_apply)
    finally:
        if cleanup and staging_path is not None:
            try:
                staging_path.unlink()
            except Exception:
                pass


@app.get("/build/status")
def build_status_endpoint() -> dict:
    """Current version, recent changelog (last N addenda from MERGE_NOTES), gate summary."""
    from .updater import current_version, list_update_stages
    from pathlib import Path as _Path
    import re as _re
    version = current_version()
    # Find MERGE_NOTES.md: try working directory first (installed), then package-relative
    notes_path = _Path("docs/MERGE_NOTES.md")
    if not notes_path.exists():
        # Try finding it relative to this file's checkout (works in tests / alternate cwds). Not the
        # `mongoose` package __init__: in the two-repo layout that belongs to mongoose-core.
        pkg_root = _Path(__file__).resolve().parent.parent.parent
        notes_path = pkg_root / "docs" / "MERGE_NOTES.md"
    recent: list[dict] = []
    if notes_path.exists():
        txt = notes_path.read_text(encoding="utf-8")
        addenda = _re.findall(
            r"###\s+(v[\d.]+\s+addendum\s+\d+[^\n]*)\n(.*?)(?=\n###|\Z)",
            txt, _re.DOTALL
        )
        for title, body in addenda[-10:]:
            summary = body.strip().split("\n")[0][:120]
            recent.append({"title": title.strip(), "summary": summary})
        recent = list(reversed(recent))   # newest first
    staged = list_update_stages()
    return {
        "ok": True,
        "version": version,
        "recent_addenda": recent,
        "staged_updates": staged[:5],
    }


@app.post("/build/upload")
async def build_upload_endpoint(request: Request) -> dict:
    """Receive a Mongoose update .zip uploaded straight from the browser (drag-and-drop or file picker),
    so the user never has to know a filesystem path. The raw zip bytes are POSTed as the request body;
    we save them to a temp file and return its path, which the user then stages/deploys via /build/stage
    or /build/deploy with {source: <path>}. Nonce-gated and size-capped. This is what turns 'ssh in and
    run mongoose update /path/to.zip' into 'drop the zip on the page and press a button'."""
    _require_cockpit_nonce(request)
    raw = await request.body()
    if not raw:
        return {"ok": False, "error": "empty upload; the request body must be the raw .zip bytes"}
    # cap at 512MB, a Mongoose build is a couple of MB, so this is generous headroom, not a real limit
    if len(raw) > 512 * 1024 * 1024:
        return {"ok": False, "error": "upload too large (over 512MB)"}
    # quick sanity: a zip starts with 'PK'. We don't fully validate here (stage_update_candidate does the
    # real verification, correct structure, pyproject, src/mongoose), this just catches an obvious wrong file.
    if raw[:2] != b"PK":
        return {"ok": False, "error": "that doesn't look like a .zip file (expected a Mongoose build zip)"}
    import tempfile as _tmp
    from pathlib import Path as _Path
    fname = (request.headers.get("x-filename", "") or "upload.zip").strip()
    fname = _Path(fname).name or "upload.zip"
    if not fname.lower().endswith(".zip"):
        fname += ".zip"
    td = _tmp.mkdtemp(prefix="mongoose-upload-")
    dest = _Path(td) / fname
    try:
        dest.write_bytes(raw)
    except Exception as exc:
        return {"ok": False, "error": f"could not save upload: {exc}"}
    return {"ok": True, "source": str(dest), "filename": fname, "bytes": len(raw)}


@app.post("/build/stage")
async def build_stage_endpoint(request: Request) -> dict:
    """Stage a build candidate from a local path or URL.
    Body: {source: 'path/to/candidate.zip'} or {url: 'https://...'}.
    Downloads (if URL), stages, runs checks. Returns the stage_id ready for deploy."""
    _require_cockpit_nonce(request)
    body = await request.json()
    url = (body.get("url") or "").strip()
    src = (body.get("source") or "").strip()
    import tempfile as _tmp
    from pathlib import Path as _Path
    from .updater import stage_update_candidate, check_update_stage
    import httpx as _httpx
    staging_path: _Path | None = None
    cleanup = False
    if url:
        # Download the zip
        try:
            async with _httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                return {"ok": False, "error": f"Download failed: HTTP {resp.status_code}"}
            td = _tmp.mkdtemp(prefix="mongoose-stage-")
            staging_path = _Path(td) / "candidate.zip"
            staging_path.write_bytes(resp.content)
            cleanup = True
        except Exception as exc:
            return {"ok": False, "error": f"Download error: {exc}"}
    elif src:
        staging_path = _Path(src).expanduser()
        if not staging_path.exists():
            return {"ok": False, "error": f"File not found: {staging_path}"}
    else:
        return {"ok": False, "error": "Provide 'url' or 'source'"}
    try:
        # stage_update_candidate (unzips, verifies) and check_update_stage (runs compileall + pyflakes
        # as subprocesses, up to 90s each) are BLOCKING. Running them directly in this async handler
        # freezes the whole event loop, every endpoint including /health times out until they finish,
        # which is exactly the "server unresponsive right after staging" wedge. Run them in a thread.
        import asyncio as _asyncio
        def _stage_and_check() -> dict:
            st = stage_update_candidate(staging_path)
            if not st.get("ok") and not st.get("stage_id"):
                return {"ok": False, "error": st.get("error", "staging failed"), "detail": st}
            sid = st["stage_id"]
            chk = check_update_stage(sid, runtime_smoke=False)
            return {"ok": True, "stage_id": sid, "version": st.get("version"),
                    "checks": chk.get("checks", []), "ready": chk.get("ok", False)}
        return await _asyncio.to_thread(_stage_and_check)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if cleanup and staging_path and staging_path.exists():
            try:
                import shutil as _sh
                _sh.rmtree(str(staging_path.parent), ignore_errors=True)
            except Exception:
                pass


@app.post("/build/deploy")
async def build_deploy_endpoint(request: Request) -> dict:
    """One-click deploy: stage + check + approve + apply a candidate.
    Body: {url: '...'}, {source: '/path/to/candidate.zip'}, or {stage_id: '...'}
    (use an already-staged candidate). This is the 'single button deploy': it calls
    the full safe update path (backup → apply → verify) and returns the result.
    Nonce-gated.

    The browser drag/drop path first uploads the zip and receives a temporary local
    `source` path; accepting `source` here keeps the UI and API contract coherent.
    Without it, the delightful upload flow staged fine but failed at "Update now".
    """
    _require_cockpit_nonce(request)
    body = await request.json()
    stage_id = (body.get("stage_id") or "").strip()
    url = (body.get("url") or "").strip()
    src = (body.get("source") or "").strip()
    from .updater import (approve_update_stage, apply_update_stage,
                          check_update_stage, stage_update_candidate)
    import asyncio as _asyncio
    import tempfile as _tmp
    import httpx as _httpx
    from pathlib import Path as _Path

    # Stage if needed. Mirror /build/stage exactly: URL downloads and local paths
    # both produce a zip that stage_update_candidate validates structurally.
    if not stage_id:
        zp: _Path | None = None
        if url:
            try:
                async with _httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                    resp = await client.get(url)
                if resp.status_code != 200:
                    return {"ok": False, "error": f"Download failed: HTTP {resp.status_code}"}
                td = _tmp.mkdtemp(prefix="mongoose-deploy-")
                zp = _Path(td) / "candidate.zip"
                zp.write_bytes(resp.content)
            except Exception as exc:
                return {"ok": False, "error": f"Download error: {exc}"}
        elif src:
            zp = _Path(src).expanduser()
            if not zp.exists():
                return {"ok": False, "error": f"File not found: {zp}"}
        else:
            return {"ok": False, "error": "Provide 'url', 'source', or 'stage_id'"}
        try:
            stage = await _asyncio.to_thread(lambda: stage_update_candidate(zp))
            stage_id = stage.get("stage_id", "")
            if not stage_id:
                return {"ok": False, "error": "staging failed", "detail": stage}
        except Exception as exc:
            return {"ok": False, "error": f"staging error: {exc}"}

    # Check, runs compileall + pyflakes as subprocesses (blocking), so MUST be off the event loop or
    # it freezes /health and every other endpoint while it runs.
    checks = await _asyncio.to_thread(lambda: check_update_stage(stage_id, runtime_smoke=False))
    if not checks.get("ok"):
        return {"ok": False, "stage_id": stage_id, "error": "checks failed",
                "checks": checks.get("checks", [])}

    # Approve and apply in a thread (apply_update_stage is blocking)
    approve_update_stage(stage_id)
    rc = await _asyncio.to_thread(lambda: apply_update_stage(stage_id, yes=True))
    if rc == 0:
        return {"ok": True, "stage_id": stage_id, "message":
                "Deploy complete. The server will restart; give it ~10 seconds then hard-refresh."}
    return {"ok": False, "stage_id": stage_id, "error": f"apply returned code {rc}"}


# ── WordPress plugin workflow ────────────────────────────────────────────────────────────────────

@app.get("/build/wordpress/status")
def wp_status_endpoint() -> dict:
    """Check WordPress connectivity, auth, and list installed plugins. Read-only."""
    from .connectors.wordpress import wp_status
    from .services.wp_plugin import wp_list_plugins, plugin_source_status, list_builds
    conn = wp_status()
    src = plugin_source_status()
    builds = list_builds()
    plugins = wp_list_plugins() if conn.get("ok") else {"plugins": [], "ok": False}
    return {"ok": conn.get("ok", False), "connection": conn, "source": src,
            "builds": builds[:5], "installed_plugins": plugins.get("plugins", [])}


@app.post("/build/wordpress/connect")
async def wp_connect_endpoint(request: Request) -> dict:
    """Save the WordPress connection (URL + username + application password) to .env, apply it to the
    running process, and live-probe it. Body: {url, username, app_password}. Nonce-gated.

    This exists so the owner connects WordPress from the Cockpit instead of hand-editing .env. The old
    'add WP_URL... to .env' instruction was a terminal journey that let the two sides drift apart: the
    plugin got configured on WordPress while Mongoose's side stayed blank, so Mongoose could not see it."""
    _require_cockpit_nonce(request)
    import os as _os
    body = await request.json()
    url = str(body.get("url") or "").strip().rstrip("/")
    user = str(body.get("username") or "").strip()
    pwd = str(body.get("app_password") or "").strip()
    if not (url and user and pwd):
        return {"ok": False, "reason": "Enter the site URL, a WordPress username, and an application password."}
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "reason": "Site URL must start with https:// (or http://)."}
    if any(ch in (url + user + pwd) for ch in ("\n", "\r")):
        return {"ok": False, "reason": "Values must each be on a single line."}
    from .setup_wizard import write_env
    write_env({"WP_URL": url, "WP_USERNAME": user, "WP_APP_PASSWORD": pwd})
    # Apply to the running process so the probe below sees it with no restart: get_settings() builds a
    # fresh Settings from os.environ on every call, so the new values are picked up immediately.
    _os.environ["WP_URL"] = url
    _os.environ["WP_USERNAME"] = user
    _os.environ["WP_APP_PASSWORD"] = pwd
    from .connectors.wordpress import wp_status
    st = wp_status()
    return {"ok": bool(st.get("ok")), "connection": st}


# Core (RTM / search / files / email / voicemail) routes live in mongoose-core.
from .rtm_api import router as _core_router  # noqa: E402
from .web_common import mount_routes as _mount_routes  # noqa: E402
_mount_routes(app, _core_router)


# Phone/browser large-file staging feeds the assistant importers (body data, knowledge import), so these
# routes live with the assistant rather than in the core API.
@app.post("/files/stage")
async def files_stage_route(request: Request) -> dict:
    """Large-file staging (<= 200MB) from the browser. Stored + classified, never auto-applied.
    Owner-gated."""
    _require_token_or_cockpit_nonce(request)
    from .services.uploads import handle_staged_upload
    filename, data, mime = await _read_upload(request)
    if data is None:
        return {"ok": False, "error": "no file received"}
    return handle_staged_upload(filename, data, uploaded_by="owner", surface="cockpit-staging",
                                mime_type=mime)


@app.get("/files/staged")
def files_staged_route(request: Request, limit: int = 50) -> dict:
    """List staged large files (the Files & staging area). Owner-gated."""
    _require_token_or_cockpit_nonce(request)
    from .services.uploads import list_staged
    return list_staged(limit=limit)











@app.get("/cockpit/fortunes")
def cockpit_fortunes(request: Request) -> dict:
    """Read-only browse view for the fortune-cookie PDFs. They are otherwise only reachable via one-time
    chat download tokens; once the chat line scrolls away the file (still on disk) is unreachable. Lists
    each PDF (name, size, mtime) with a freshly minted capability download link. Owner-gated."""
    _require_cockpit_nonce(request)
    from datetime import datetime, timezone
    from .settings import get_settings
    from .services.rtm_templates import mint_download
    d = get_settings().db_path.parent / "fortunes"
    items: list[dict] = []
    try:
        files = sorted(d.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []
    except Exception:
        files = []
    for f in files:
        try:
            stt = f.stat()
        except Exception:
            continue
        entry = {"name": f.name, "size": stt.st_size,
                 "mtime": datetime.fromtimestamp(stt.st_mtime, tz=timezone.utc).isoformat()}
        try:
            tok = mint_download(str(f), f.name, ttl_s=3600, media_type="application/pdf")
            entry["download_url"] = f"/rtm/doc/{tok}"
        except Exception:
            entry["download_url"] = ""
        items.append(entry)
    return {"ok": True, "count": len(items), "fortunes": items}


@app.get("/cockpit/receipts-issued")
def cockpit_receipts_issued(request: Request) -> dict:
    """Read-only ledger of every issued RTM trust receipt. Individual receipts are otherwise downloadable
    only if you already know the flat + document number; this lists them all with a ready download link
    (via the existing /rtm/receipt/download route). Records only; never moves money. Owner-gated."""
    _require_cockpit_nonce(request)
    from urllib.parse import quote
    from .services import rtm_trust as TR
    try:
        rows = TR.list_receipts(None)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "receipts": []}
    out: list[dict] = []
    for r in rows:
        flat = str(r.get("flat_id") or "")
        doc = str(r.get("document_number") or "")
        item = {
            "flat_id": flat,
            "document_number": doc,
            "receipt_number": r.get("receipt_number") or r.get("receipt_no") or "",
            "amount": r.get("amount"),
            "created_at": r.get("created_at") or r.get("issued_at") or "",
        }
        if flat and doc:
            item["download_url"] = f"/rtm/receipt/download?flat={quote(flat)}&document={quote(doc)}"
        out.append(item)
    if not out:
        # The trust-receipts ledger can be empty even though receipts WERE generated for the portal (the
        # leaseholder actually got one). Fall back to the generated portal receipt PDFs so the list reflects
        # reality; join each to the flat's paid demand so the download link still resolves.
        try:
            from .db import connect
            with connect() as con:
                prows = con.execute(
                    "SELECT p.receipt_no, p.flat_id, p.amount, p.received_date, p.created_at, "
                    "(SELECT d.document_number FROM rtm_mgmt_demands d WHERE d.flat_id=p.flat_id "
                    " AND d.status='Paid' ORDER BY d.updated_at DESC LIMIT 1) AS document_number "
                    "FROM portal_receipt_pdfs p ORDER BY p.created_at DESC").fetchall()
            for r in prows:
                d = dict(r)
                flat = str(d.get("flat_id") or "")
                doc = str(d.get("document_number") or "")
                item = {"flat_id": flat, "document_number": doc,
                        "receipt_number": d.get("receipt_no") or "",
                        "amount": d.get("amount"),
                        "created_at": d.get("received_date") or (str(d.get("created_at") or "")[:10])}
                if flat and doc:
                    item["download_url"] = f"/rtm/receipt/download?flat={quote(flat)}&document={quote(doc)}"
                out.append(item)
        except Exception:
            pass
    return {"ok": True, "count": len(out), "receipts": out}


@app.get("/cockpit/demand-mailing")
def cockpit_demand_mailing_preview(request: Request, period: str = "") -> dict:
    """Read-only dry-run of the batch payment-now-due mailing: who would be emailed, the amounts and
    references, who is excluded and why, and whether each is ready to send. Sends nothing. Owner-gated.
    The confirmed send is a separate POST so a preview can never send by accident."""
    _require_cockpit_nonce(request)
    from .services import rtm_demand_mailing as DM
    try:
        prep = DM.prepare(period.strip() or None)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, **prep,
            "sent_already": DM.sent_log(prep["period"], limit=500)}


@app.post("/cockpit/demand-mailing/send")
async def cockpit_demand_mailing_send(request: Request) -> dict:
    """Perform the batch send, one recipient at a time, each with its own demand PDF. Owner-gated AND
    requires an explicit {"confirm": true} in the body, so nonce-alone can never trigger a real send.
    Idempotent: a demand already emailed this period is skipped. Body: {confirm, period?, force?}."""
    _require_cockpit_nonce(request)
    body = await request.json()
    if not bool(body.get("confirm")):
        return {"ok": False, "error": "confirm:true is required to send. This is a real mailing to leaseholders."}
    import asyncio as _asyncio
    from .services import rtm_demand_mailing as DM
    period = str(body.get("period") or "").strip() or None
    return await _asyncio.to_thread(DM.send_batch, period, confirm=True, force=bool(body.get("force")),
                                    priority=str(body.get("priority") or "normal"))


@app.get("/cockpit/demand-mailing/log")
def cockpit_demand_mailing_log(request: Request, period: str = "") -> dict:
    """The per-recipient send log for the demand mailing (sent/failed, when, message id). Owner-gated."""
    _require_cockpit_nonce(request)
    from .services import rtm_demand_mailing as DM
    return {"ok": True, "log": DM.sent_log(period.strip() or None, limit=500)}


























@app.get("/build/wordpress/posts")
def wp_posts_endpoint(request: Request, limit: int = 10, status: str = "any") -> dict:
    """List recent WordPress posts. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .connectors.wordpress import list_posts
    return list_posts(limit=limit, status=status)


@app.post("/build/wordpress/post")
async def wp_create_post_endpoint(request: Request) -> dict:
    """Create a WordPress post (draft by default).
    Body: {title, content, status ('draft'|'publish'), excerpt}"""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .connectors.wordpress import create_post
    return create_post(
        title=str(body.get("title") or ""),
        content=str(body.get("content") or ""),
        status=str(body.get("status") or "draft"),
        excerpt=str(body.get("excerpt") or ""),
    )


@app.post("/build/wordpress/post/{post_id}")
async def wp_update_post_endpoint(post_id: int, request: Request) -> dict:
    """Update an existing WordPress post."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .connectors.wordpress import update_post
    return update_post(post_id, title=body.get("title"),
                       content=body.get("content"), status=body.get("status"))

@app.get("/build/plugin/source")
def plugin_source_endpoint() -> dict:
    """Local plugin source directory status."""
    from .services.wp_plugin import plugin_source_status
    return plugin_source_status()



@app.post("/build/plugin/scaffold")
async def plugin_scaffold_endpoint(request: Request) -> dict:
    """Generate a starter RTM plugin (named for this site's RTM) in MONGOOSE_PLUGIN_DIR.
    Body: {version: '0.1.0'}: optional version string."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .services.wp_plugin import scaffold_example_court_plugin
    return scaffold_example_court_plugin(str(body.get("version") or "0.1.0"))

@app.get("/build/plugin/download/{name}")
def plugin_download_endpoint(request: Request, name: str):
    """Download a previously built plugin zip. A build has always been listable (list_builds())
    but never actually retrievable through the cockpit -- built successfully, then nowhere to
    get it. Serves ONLY a filename that list_builds() itself reports right now, never a raw path
    from the request, so this can't be turned into a path-traversal read of arbitrary files."""
    _require_cockpit_nonce(request)
    from .services.wp_plugin import list_builds
    from fastapi.responses import FileResponse
    match = next((b for b in list_builds() if b["name"] == name), None)
    if match is None:
        raise HTTPException(status_code=404, detail="No such build. Refresh the Build tab and try again.")
    return FileResponse(match["path"], media_type="application/zip", filename=match["name"])


@app.post("/build/plugin/build")
async def plugin_build_endpoint(request: Request) -> dict:
    """Build the plugin source into a deployable zip.
    Body: {version_suffix: 'beta'}: suffix appended to the zip filename."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .services.wp_plugin import build_plugin_zip
    return build_plugin_zip(version_suffix=str(body.get("version_suffix") or "beta"))


@app.post("/build/plugin/deploy-beta")
async def plugin_deploy_beta_endpoint(request: Request) -> dict:
    """Build + upload to WordPress (inactive). Body: {version_suffix, zip_path (optional)}."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .services.wp_plugin import deploy_plugin_beta
    return deploy_plugin_beta(
        version_suffix=str(body.get("version_suffix") or "beta"),
        zip_path=body.get("zip_path") or None,
    )


@app.post("/build/plugin/activate")
async def plugin_activate_endpoint(request: Request) -> dict:
    """Activate or deactivate a plugin. Body: {plugin: 'slug/main.php', status: 'active'|'inactive'}."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .services.wp_plugin import wp_set_plugin_status
    return wp_set_plugin_status(str(body.get("plugin") or ""), str(body.get("status") or "active"))


@app.post("/build/plugin/promote")
async def plugin_promote_endpoint(request: Request) -> dict:
    """Promote beta to production (activate beta, deactivate old).
    Body: {beta_slug: '...', production_slug: '...'}."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .services.wp_plugin import promote_plugin
    return promote_plugin(str(body.get("beta_slug") or ""), str(body.get("production_slug") or ""))


@app.delete("/build/plugin/{plugin_slug:path}")
async def plugin_delete_endpoint(plugin_slug: str, request: Request) -> dict:
    """Delete (uninstall) an inactive plugin from WordPress."""
    _require_cockpit_nonce(request)
    from .services.wp_plugin import wp_delete_plugin
    return wp_delete_plugin(plugin_slug)


@app.get("/build/plugin/builds")
def plugin_builds_endpoint() -> dict:
    """List previously built plugin zips."""
    from .services.wp_plugin import list_builds
    return {"ok": True, "builds": list_builds()}


# ── Webhook (inbound from example.org and other external sites) ──────────────────────────────

@app.get("/webhook/setup")
def webhook_setup_endpoint() -> dict:
    """Public read: webhook configuration status: is a secret set, what is the ingest URL.
    The cockpit uses this to show the configuration card and copy-pasteable values."""
    from .ddns import public_url
    from .services.webhook import webhook_configured, public_webhook_url
    base = public_url()
    wh_url = public_webhook_url()
    return {
        "ok": True,
        "configured": webhook_configured(),
        "ingest_url": wh_url,
        "public_base": base,
        "ddns_needed": base is None,
        "secret_hint": "(set in .env as MONGOOSE_WEBHOOK_SECRET)",
    }


@app.post("/webhook/generate-secret")
async def webhook_generate_secret_endpoint(request: Request) -> dict:
    """Generate a new webhook secret and persist it to .env. Nonce-gated.
    After generation the ingest URL and the secret are shown in the cockpit so they can be
    copied directly into the WordPress plugin settings."""
    _require_cockpit_nonce(request)
    from .services.webhook import generate_webhook_secret as _gen_wh_secret, public_webhook_url
    from .setup_wizard import write_env
    secret = _gen_wh_secret()
    try:
        write_env({"MONGOOSE_WEBHOOK_SECRET": secret})
    except Exception as exc:
        return {"ok": False, "error": f"could not write .env: {exc}"}
    # Also invalidate settings cache so the new secret is live immediately
    try:
        import mongoose.settings as _S
        if hasattr(_S, "_settings"): _S._settings = None
        if hasattr(_S.get_settings, "cache_clear"): _S.get_settings.cache_clear()
    except Exception:
        pass
    return {
        "ok": True,
        "secret": secret,
        "ingest_url": public_webhook_url(),
        "message": "Webhook secret generated. Copy the secret and ingest URL into the WordPress plugin settings.",
    }


def _mcp_static_token() -> str:
    """A static bearer token accepted by /mcp IN ADDITION to OAuth access tokens, handy for owner curl
    tests and any client that supports a fixed header. Prefers a dedicated MONGOOSE_MCP_TOKEN."""
    import os as _os
    return (_os.getenv("MONGOOSE_MCP_TOKEN") or _os.getenv("MONGOOSE_API_TOKEN") or "").strip()


def _mcp_authorized(request: Request) -> bool:
    import secrets as _secrets
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if not token:
        return False
    static = _mcp_static_token()
    if static and _secrets.compare_digest(token, static):
        return True
    from .services.mcp_oauth import valid_access_token
    return valid_access_token(token)


# ── OAuth authorization server (minimal, for the Claude.ai MCP connector) ────────
@app.get("/.well-known/oauth-authorization-server")
@app.get("/.well-known/oauth-authorization-server/mcp")
@app.get("/.well-known/openid-configuration")
def mcp_oauth_as_metadata():
    from .services.mcp_oauth import authorization_server_metadata
    return authorization_server_metadata()


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
def mcp_oauth_pr_metadata():
    from .services.mcp_oauth import protected_resource_metadata
    return protected_resource_metadata()


@app.get("/authorize")
def mcp_oauth_authorize(request: Request):
    from fastapi.responses import RedirectResponse, JSONResponse
    from .services.mcp_oauth import authorize
    res = authorize(dict(request.query_params))
    if res.get("ok"):
        return RedirectResponse(url=res["redirect"], status_code=302)
    return JSONResponse(status_code=400, content={"error": res.get("error"), "error_description": res.get("detail")})


@app.post("/token")
async def mcp_oauth_token(request: Request):
    from fastapi.responses import JSONResponse
    from .services.mcp_oauth import exchange
    # Parse the urlencoded (or JSON) token body directly, so we do not depend on python-multipart.
    form: dict[str, str] = {}
    try:
        raw = (await request.body()).decode("utf-8", "ignore")
        ctype = request.headers.get("content-type", "")
        if "application/json" in ctype:
            import json as _json
            form = {str(k): str(v) for k, v in (_json.loads(raw) or {}).items()}
        else:
            from urllib.parse import parse_qsl
            form = dict(parse_qsl(raw))
    except Exception:
        form = {}
    basic = None
    authz = request.headers.get("authorization", "")
    if authz[:6].lower() == "basic ":
        import base64 as _b64
        try:
            u, _, p = _b64.b64decode(authz[6:]).decode().partition(":")
            basic = (u, p)
        except Exception:
            basic = None
    result = exchange({k: str(v) for k, v in form.items()}, basic_auth=basic)
    status = 200 if result.get("access_token") else 400
    return JSONResponse(status_code=status, content=result,
                        headers={"Cache-Control": "no-store"})


@app.post("/register")
async def mcp_oauth_register(request: Request):
    from fastapi.responses import JSONResponse
    from .services.mcp_oauth import register
    try:
        body = await request.json()
    except Exception:
        body = {}
    return JSONResponse(status_code=201, content=register(body if isinstance(body, dict) else {}))


@app.api_route("/mcp", methods=["GET", "POST"])
async def mcp_endpoint(request: Request):
    """Mongoose MCP server for the Claude.ai custom connector. JSON-RPC 2.0 over HTTP, gated by an OAuth
    access token (or a static MONGOOSE_MCP_TOKEN), READ-ONLY tools. GET (SSE stream open) is not offered
, request/response only, so it returns 405 per the Streamable HTTP spec."""
    from fastapi.responses import JSONResponse, Response
    if not _mcp_authorized(request):
        # Point the client at our protected-resource metadata so it runs the OAuth flow (RFC 9728).
        from . import profile as _profile
        _pub = __import__('os').getenv('MONGOOSE_PUBLIC_URL')
        _pub = _pub if _pub is not None else _profile.public_url()
        rm = f"{_pub.rstrip('/')}/.well-known/oauth-protected-resource"
        return JSONResponse(status_code=401, content={"error": "unauthorized"},
                            headers={"WWW-Authenticate": f'Bearer resource_metadata="{rm}"'})
    if request.method == "GET":
        # No server-initiated SSE stream; single request/response only.
        return Response(status_code=405, headers={"Allow": "POST"})
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400,
                            content={"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32700, "message": "parse error"}})
    import asyncio as _asyncio
    from .services.mcp_server import handle
    resp = await _asyncio.to_thread(handle, payload)  # tools hit the DB; keep off the event loop
    if resp is None:
        return Response(status_code=202)  # notification(s): accepted, nothing to return
    return JSONResponse(content=resp)


@app.post("/webhook/ingest")
async def webhook_ingest_endpoint(request: Request) -> dict:
    """Receive a signed event from the RTM site (or any configured external site).
    Verifies HMAC-SHA256 signature from X-Mongoose-Signature header. Public endpoint
    (no login required, but signature is mandatory)."""
    body = await request.body()
    sig = request.headers.get("X-Mongoose-Signature", "")
    from .services.webhook import verify_signature, webhook_configured, ingest_webhook_event
    if not webhook_configured():
        return {"ok": False, "error": "webhook_not_configured",
                "hint": "Generate a webhook secret in cockpit Settings → Webhook."}
    if not verify_signature(body, sig):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=401,
                            content={"ok": False, "error": "invalid_signature"})
    try:
        payload = __import__("json").loads(body)
    except Exception:
        return {"ok": False, "error": "invalid_json_body"}
    event = str(payload.get("event") or "unknown").strip()
    source = str(payload.get("source") or request.headers.get("X-Site-Domain", "external")).strip()
    # A pure reachability + signature probe from the WordPress "Test webhook" button. Side-effect-free:
    # it drains no queue and records nothing, it just confirms the URL is right and the secret matches
    # (getting here at all means the HMAC verified). Echoes the version so the button can show it.
    if event in ("webhook_test", "ping"):
        from . import __version__ as _ver
        return {"ok": True, "event": event, "pong": True, "version": _ver, "source": source}
    # WordPress "Seller pack" page: the director asked (over the signed bridge) for the STAGE-1 LPE1
    # reply for a flat. Generate the branded cover letter + statutory admin-charge summary and return
    # absolute, browser-openable /rtm/doc links (public-prefixed, token is the auth) so the plugin can
    # show print buttons. Artifacts only: nothing is sent and the completed form is never released here.
    if event == "seller_pack_first_stage":
        import asyncio as _aio
        from .services import rtm_seller_pack_response as _R
        flat = str(payload.get("flat") or "").strip()
        if not flat:
            return {"ok": False, "error": "flat required"}
        _job = {k: str(payload[k]).strip() for k in ("seller_name", "conveyancer_firm",
                "conveyancer_contact", "conveyancer_email", "their_ref", "request_date") if payload.get(k)}
        if payload.get("save") and _job:
            await _aio.to_thread(_R.set_job, flat, **_job)
        res = await _aio.to_thread(lambda: _R.first_stage_response(flat, download_ttl_s=86400, **_job))
        # absolutise the download links to the origin the request arrived on (the tunnel host)
        _host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        _proto = request.headers.get("x-forwarded-proto") or "https"
        _base = f"{_proto}://{_host}" if _host else ""
        if isinstance(res, dict):
            for _k in ("cover_letter", "admin_charge_summary"):
                _d = res.get(_k)
                if isinstance(_d, dict) and str(_d.get("download_url", "")).startswith("/"):
                    _d["url"] = _base + _d["download_url"]
        return res
    # "You have work" nudge from WordPress: a director queued an action (mark-paid, generate document,
    # membership change) and is asking Mongoose to pull it now instead of waiting for the timer. Drain the
    # queues in the BACKGROUND and answer immediately, so WordPress is never blocked on the pull (which
    # makes 20-40s sync calls back to WordPress). The scheduler timer is the safety net if this is missed.
    if event in ("rtm_queue_ready", "queue_ready", "sync_now"):
        import asyncio as _aio
        async def _drain() -> None:
            try:
                from .services.rtm_portal import sync_wp_queues
                await _aio.to_thread(sync_wp_queues)
            except Exception:
                pass
        _aio.get_event_loop().create_task(_drain())
        return {"ok": True, "event": event, "queued": True}
    # Home Assistant RTM voicemail add-on staging a voicemail transcript. HA watches the same voip_provider
    # IMAP mailbox and transcribes on notification; this feeds its transcript into Mongoose as the
    # 'ha_staging' source, which the AI judge/merge weighs against the local Whisper transcript.
    # Backgrounded because the merge may invoke the local LLM; HA does not need the merge result.
    if event in ("voicemail_transcript", "ha_voicemail_transcript"):
        import asyncio as _aio
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        async def _stage() -> None:
            try:
                from .services.voicemail import stage_transcript
                await _aio.to_thread(
                    stage_transcript,
                    transcript=str(data.get("transcript") or ""),
                    provider_uid=data.get("provider_uid") or data.get("uid"),
                    # the HA add-on sends `attachment_filename` as a match hint; accept it too, since the
                    # voip_provider UUID is embedded in the .wav filename and our identity key extracts it.
                    audio_filename=(data.get("audio_filename") or data.get("filename")
                                    or data.get("attachment_filename")),
                    caller_number=data.get("caller_number") or data.get("caller"),
                    account_number=data.get("account_number") or data.get("account"),
                    voicemail_at=data.get("voicemail_at") or data.get("received_at"),
                    source_message_id=data.get("source_message_id") or data.get("message_id"),
                    source=str(data.get("source") or "ha_staging"),
                )
            except Exception:
                pass
        _aio.get_event_loop().create_task(_stage())
        return {"ok": True, "event": event, "staged": True}
    return ingest_webhook_event(event, source, payload)


@app.get("/system/status")
def system_status_endpoint() -> dict:
    return _status_cached("system_status", 20.0, system_status)


@app.get("/system/resources")
def system_resources_endpoint(include_process_table: bool = False) -> dict:
    """Local OS/process pressure snapshot: memory, loop lag, scheduler heartbeat, and optional
    Mongoose/Ollama process list. Read-only and owner-gated by the normal system-route boundary."""
    from .services.resource_guard import assess_runtime_pressure
    return assess_runtime_pressure(include_process_table=include_process_table)


@app.get("/cockpit/models")
async def cockpit_models() -> dict:
    """Model architecture + residency for the cockpit Models tab, plus the per-role provenance
    view (what each role is for, which model is in effect, and where that model came from)."""
    from .model_residency import check_residency
    res = await check_residency()
    try:
        from .model_tuning import model_roles_overview
        from .benchmark import installed_models
        installed = await installed_models()
        res["roles"] = model_roles_overview(installed=installed)
        res["how_to_change"] = (
            "Mongoose picks sensible defaults, then benchmarks overnight and can suggest or apply "
            "a better model per role (always reversible). To pin a role yourself, set its variable "
            "in your .env (OLLAMA_CHAT_MODEL, OLLAMA_TOOL_MODEL, OLLAMA_CODE_MODEL, "
            "OLLAMA_RESEARCH_MODEL) and restart. See docs/MODEL_ROLES.md.")
    except Exception:
        pass
    return res


@app.post("/cockpit/models/pull")
async def cockpit_models_pull(request: Request) -> dict:
    """Install (pull) an Ollama model in the background. Owner-only (nonce-gated). Returns
    immediately; the UI polls /cockpit/models/pull-status. This downloads several GB, so it is
    always an explicit owner action, never automatic."""
    _require_cockpit_nonce(request)
    from .services.model_install import start_install
    payload = await request.json()
    return start_install(str(payload.get("model") or ""))


@app.get("/cockpit/models/pull-status")
def cockpit_models_pull_status(model: str = "") -> dict:
    """Progress of an in-flight model install (state: running/done/error, plus percent)."""
    from .services.model_install import install_state
    return install_state(model)


@app.post("/cockpit/embeddings/install")
async def cockpit_embeddings_install(request: Request) -> dict:
    """One-click install of semantic search: pip-install the CPU runtime libs and download the
    model files if missing. Owner-only (nonce-gated). Poll /cockpit/models/pull-status?model=embeddings."""
    _require_cockpit_nonce(request)
    from .services.model_install import start_embeddings_install
    return start_embeddings_install()


@app.get("/cockpit/embeddings-benchmark")
def cockpit_embeddings_benchmark() -> dict:
    """Embeddings (semantic search) benchmark: is it available, how fast on this Mac, and does
    the cross-lingual gist match work. One local ONNX model, so this is a health+speed check,
    not a model comparison."""
    from .services.embeddings import benchmark
    return benchmark()


@app.get("/cockpit/scheduler")
def cockpit_scheduler() -> dict:
    """Scheduler job status for the cockpit Scheduler tab."""
    from .services.scheduler import job_status, _scheduler_running
    return {"jobs": job_status(), "running": _scheduler_running()}


@app.post("/cockpit/scheduler/control")
async def cockpit_scheduler_control(request: Request) -> dict:
    """Pause/resume background jobs from the UI using the same durable control plane chat/CLI use.
    This is intentionally nonce-gated: even 'free memory' changes long-running local behaviour."""
    _require_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    from .services.scheduler import scheduler_control
    return scheduler_control(
        str(body.get("action") or "status"),
        jobs=body.get("jobs"),
        reason=str(body.get("reason") or "cockpit scheduler control"),
        actor="cockpit",
    )


@app.get("/cockpit/guests/privacy")
def cockpit_guest_privacy(request: Request) -> dict:
    """Privacy-preserving guest boundary report: counts + checks, no guest content or secrets."""
    _require_cockpit_nonce(request)
    from .services.guest_privacy import guest_privacy_report
    return guest_privacy_report()


async def _enable_phone_and_schedule_restart(payload: dict) -> dict:
    """Shared body for the two enable-phone routes (cockpit nonce-gated, and the loopback-only install
    route). Writes MONGOOSE_HOST=0.0.0.0 + a token to .env, then schedules a detached restart so
    uvicorn rebinds to the LAN (writing config is not enough; only a restart makes the running
    process rebind). Restart mechanism is shared with _repair_phone_reachability's auto-repair path
    via _schedule_detached_phone_restart, so the button and the pairing-flow repair can never drift
    into two different restart implementations again."""
    from .enable_phone import ensure_phone_access_defaults
    # Repair mode: restart even if config already says enabled. Fixes the real bug where a process
    # started earlier on 127.0.0.1 keeps serving loopback-only even though .env now says 0.0.0.0.
    force_restart = bool(payload.get("force") or payload.get("repair"))
    s = get_settings()
    config_says_enabled = (getattr(s, "host", "127.0.0.1") in ("0.0.0.0", "::")
                           and getattr(s, "allow_remote", False) and bool(getattr(s, "api_token", "")))
    # CONFIG alone is not proof: a process started before .env said 0.0.0.0 is still genuinely
    # loopback-only until restarted, and trusting config here was exactly the bug that silently
    # left a phone's QR pointing at a LAN address nothing was listening on. Verify the RUNNING
    # process with a real TCP probe against its own LAN IP before ever skipping the restart.
    already = config_says_enabled and (_lan_socket_reachable(s) if config_says_enabled else False)
    try:
        ensure_phone_access_defaults()
    except Exception as exc:
        return {"ok": False, "error": f"Could not write settings: {exc}"}
    if already and not force_restart:
        return {"ok": True, "already": True,
                "detail": "LAN access is configured and the running listener is reachable on the "
                          "LAN; no repair needed."}
    restart = _schedule_detached_phone_restart()
    if not restart.get("ok"):
        return {"ok": False, "error": restart.get("error") or "Could not restart Mongoose for phone access."}
    return {"ok": True, "restarting": True,
            "detail": "Enabling network access and restarting Mongoose. This page will reconnect "
                      "and then the pairing link will work on your phone."}


@app.post("/cockpit/enable-phone")
async def cockpit_enable_phone(request: Request) -> dict:
    """Enable LAN access for phone/tablet pairing from the cockpit (the one-click version of
    `mongoose enable-phone`). Nonce-gated. Stays on the local network; does not expose to the internet."""
    _require_cockpit_nonce(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return await _enable_phone_and_schedule_restart(payload)


@app.post("/install/enable-phone")
async def install_enable_phone(request: Request) -> dict:
    """First-run, loopback-only enable-phone for the public /install page. The install page is
    deliberately public before any account/device exists, so it cannot call the nonce-gated cockpit
    action (there is no cockpit nonce yet). This route is narrower: only the Mac itself (loopback) may
    call it, and it disappears once any device has paired."""
    client_host = request.client.host if request.client else ""
    if client_host not in _LOOPBACK_HOSTS:
        raise HTTPException(status_code=403, detail="local only")
    from .onboarding import _paired_count
    if _paired_count() > 0:
        raise HTTPException(status_code=403, detail="phone access is managed from the cockpit after setup")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return await _enable_phone_and_schedule_restart(payload)


@app.post("/cockpit/restart")
async def cockpit_restart(request: Request) -> dict:
    """Restart the Mongoose server from the browser (the watchdog's 'Restart the assistant' action,
    and a general recovery button). Stops the current server and relaunches it detached, the same
    mechanism enable-phone uses. Returns FIRST, then restarts a moment later so the response flushes.
    Nonce-gated. NOTE: if the event loop is fully wedged this coroutine won't run either, the UI
    watchdog shows a manual terminal fallback for that case. This covers the common 'busy/slow but
    alive' case where a clean restart clears it."""
    _require_cockpit_nonce(request)
    import sys
    import subprocess
    import asyncio as _asyncio
    async def _restart_soon():
        # Hand the stop+relaunch to a DETACHED helper. The old code ran `mongoose stop` INLINE here ,
        # but stop kills THIS process, so the line that relaunched serve never executed and the server
        # stayed dead. The helper runs in its own session (start_new_session), so it outlives this
        # server being killed and brings a fresh one up. The external watchdog is a second safety net.
        await _asyncio.sleep(0.4)   # let the HTTP response flush first
        from .service import _repo_root, _log_dir
        install_dir = str(_repo_root())
        log_file = _log_dir() / "app.log"
        port = str(get_settings().port)
        helper = (
            "import time,subprocess,sys\n"
            "time.sleep(1.2)\n"
            "try:\n"
            "    subprocess.run([sys.executable,'-m','mongoose.cli','stop'],cwd=%r,timeout=30)\n"
            "except Exception:\n"
            "    pass\n"
            "subprocess.Popen([sys.executable,'-m','mongoose.cli','serve','--port',%r],"
            "cwd=%r,start_new_session=True)\n"
        ) % (install_dir, port, install_dir)
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "ab") as log:
                subprocess.Popen([sys.executable, "-c", helper],
                                 stdout=log, stderr=log, start_new_session=True, cwd=install_dir)
        except Exception:
            pass

    _asyncio.create_task(_restart_soon())
    return {"ok": True, "restarting": True,
            "detail": "Restarting Mongoose. This page will reload in a few seconds."}


@app.get("/cockpit/supervisor")
def cockpit_supervisor() -> dict:
    """Is a server supervisor (the external watchdog) actually running right now? The cockpit warns
    when it isn't, because without it a crash, or a failed restart, won't auto-recover."""
    from .services.server_watchdog import supervisor_status
    return supervisor_status()


@app.post("/cockpit/supervisor/start")
async def cockpit_supervisor_start(request: Request) -> dict:
    """Start the external watchdog now (detached) so the server is supervised from here on. The
    watchdog probes /health/live and relaunches the server if it dies or wedges. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services.server_watchdog import spawn_detached
    import asyncio as _asyncio
    return await _asyncio.to_thread(lambda: spawn_detached(reason="cockpit-start"))


@app.post("/cockpit/cloud-escalation")
async def cockpit_cloud_escalation(request: Request) -> dict:
    """Turn cloud escalation on/off. This was previously an env-only setting
    (CLOUD_ESCALATION_ENABLED) with no UI control anywhere -- the only way to change it was
    editing .env by hand, which contradicts every other setup step in this app. Configuring
    OpenAI/Gemini API keys does NOT imply this is on: it is a deliberate, separate safety gate
    (no turn ever reaches a paid API unless explicitly opted in), but the person needs an actual
    way to opt in without a terminal, which this provides."""
    _require_cockpit_nonce(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    enabled = bool(payload.get("enabled"))
    from .setup_wizard import write_env
    write_env({"CLOUD_ESCALATION_ENABLED": "true" if enabled else "false"})
    import os as _os_ce
    _os_ce.environ["CLOUD_ESCALATION_ENABLED"] = "true" if enabled else "false"
    return {"ok": True, "enabled": enabled,
            "detail": ("Cloud escalation is on: a turn the local model can't answer may be sent "
                       "to a configured cloud provider, within the cost cap." if enabled else
                       "Cloud escalation is off: every turn stays on the local model.")}


@app.post("/cockpit/setup-ntfy")
async def cockpit_setup_ntfy(request: Request) -> dict:
    """Set up native local ntfy (phone push) from the browser, so the user doesn't have to drop to a
    terminal to run `mongoose setup-ntfy`. Runs the same routine in a worker thread (it can take a
    while: it may brew-install ntfy and register a launchd agent). Nonce-gated. Returns whether ntfy
    ends up answering, plus the topic to subscribe to on the phone. If Homebrew/ntfy can't be
    installed non-interactively, it reports that honestly so the user knows the one manual step."""
    _require_cockpit_nonce(request)
    import platform
    if platform.system() != "Darwin":
        return {"ok": False, "error": "Native ntfy setup targets macOS. On Linux, install ntfy from your package manager."}
    try:
        import asyncio as _asyncio
        from .setup_ntfy import setup_ntfy
        # yes=True so it doesn't block on an interactive Homebrew confir[m] prompt
        rc = await _asyncio.to_thread(setup_ntfy, True)
        ok = (rc == 0)
        out = {"ok": ok}
        if ok:
            try:
                from .setup_wizard import effective_env
                out["topic"] = effective_env().get("NTFY_TOPIC", "")
                out["url"] = effective_env().get("NTFY_URL", "")
            except Exception:
                pass
            out["detail"] = "Native ntfy is set up and running. Subscribe to the topic in the ntfy phone app."
        else:
            out["error"] = ("ntfy couldn't be set up automatically. The usual cause is Homebrew isn't "
                            "installed. Install it from https://brew.sh then click this again, or run "
                            "'mongoose setup-ntfy' once in a terminal.")
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"setup failed: {exc}"}


@app.get("/cockpit/push-status")
def cockpit_push_status() -> dict:
    """Phone-push readiness for the Setup page: is Docker installed/running, and is a native ntfy
    already set up? Phone push is OPTIONAL; this just makes the state and the two paths visible so a
    user whose Docker isn't running isn't stuck with no option (the original first-run complaint)."""
    import shutil
    import subprocess
    import platform
    docker_installed = bool(shutil.which("docker"))
    docker_running = False
    if docker_installed:
        try:
            docker_running = subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
        except Exception:
            docker_running = False
    # native ntfy presence: the launch agent or the ntfy binary
    native_ntfy = bool(shutil.which("ntfy"))
    return {
        "docker_installed": docker_installed,
        "docker_running": docker_running,
        "native_ntfy": native_ntfy,
        "is_macos": platform.system() == "Darwin",
        "can_start_docker": docker_installed and platform.system() == "Darwin",
    }


@app.post("/cockpit/whatsapp/start-container")
async def cockpit_whatsapp_start_container(request: Request) -> dict:
    """Start the WhatsApp WAHA container from the browser: no terminal. Wires up
    whatsapp_journey.start_container(), which was already written to close exactly this
    gap ('you must run docker compose by hand') but had never been connected to a route the
    cockpit could actually call, so the UI kept telling people to open a terminal regardless."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.whatsapp_journey import start_container
    return await _asyncio.to_thread(start_container)


@app.post("/cockpit/start-docker")
async def cockpit_start_docker(request: Request) -> dict:
    """Launch Docker Desktop (macOS) from the browser and wait briefly for the daemon, then report
    back. Does NOT block for the full container startup (that happens on its own); just gets Docker
    itself running so the optional ntfy/SearXNG services can come up. Honest on non-macOS."""
    _require_cockpit_nonce(request)
    import shutil
    import subprocess
    import platform
    import asyncio as _asyncio
    if not shutil.which("docker"):
        return {"ok": False, "error": "Docker isn't installed. Phone push also works without it, "
                "use the native push option below."}
    # subprocess.run blocks; run every one OFF the event loop so this setup endpoint can never stall
    # the loop (a 5s `docker info` x up to 30 polls would otherwise freeze the whole server).
    def _docker_info_ok() -> bool:
        try:
            return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
        except Exception:
            return False
    if await _asyncio.to_thread(_docker_info_ok):
        return {"ok": True, "detail": "Docker is already running."}
    if platform.system() != "Darwin":
        return {"ok": False, "error": "Start the Docker daemon, then retry. On Linux: "
                "sudo systemctl start docker."}
    await _asyncio.to_thread(lambda: subprocess.run(["open", "-a", "Docker"], capture_output=True))
    # poll up to ~30s without blocking the event loop hard
    for _ in range(30):
        await _asyncio.sleep(1)
        if await _asyncio.to_thread(_docker_info_ok):
            return {"ok": True, "detail": "Docker is up. The push/search containers will start shortly."}
    return {"ok": False, "error": "Docker is taking a while to start. Open Docker Desktop, wait for "
            "'running', then retry, or use the native push option which needs no Docker."}


@app.post("/cockpit/bug/{bug_id}/dismiss")
def cockpit_bug_dismiss(bug_id: str, request: Request, body: dict = Body(default={})) -> dict:
    """Dismiss a bug from the Approvals queue without a code fix (not reproducible, guest misunderstanding)."""
    _require_cockpit_nonce(request)
    from .services.bug_workflow import dismiss_bug
    return dismiss_bug(bug_id, reason=str((body or {}).get("reason") or ""))


@app.post("/cockpit/bug/{bug_id}/diagnose")
async def cockpit_bug_diagnose(bug_id: str, request: Request, body: dict = Body(default={})) -> dict:
    """Run the deterministic + local-model diagnosis for a bug so the owner can act on it from Approvals."""
    _require_cockpit_nonce(request)
    from .services.bug_workflow import diagnose_bug
    return await diagnose_bug(bug_id, str((body or {}).get("detail") or ""))


def _kn_object_summary(object_type: str, object_id: str) -> str:
    """Best-effort human text for a queued knowledge object, so Approvals shows the actual content
    (the fact to remember, the insight, the claim) rather than only a category label."""
    try:
        from .db import connect
        table, col, key = {
            "memory_candidate": ("knowledge_memory_candidates", "fact", "memory_id"),
            "insight_candidate": ("knowledge_insight_candidates", "title", "insight_id"),
            "claim": ("knowledge_claims", "summary", "claim_id"),
        }.get(object_type, (None, None, None))
        if not table:
            return ""
        with connect() as con:
            row = con.execute(f"SELECT {col} AS v FROM {table} WHERE {key}=?", (object_id,)).fetchone()
        return str(row["v"]) if row and row["v"] else ""
    except Exception:
        return ""


@app.get("/cockpit/approvals")
def cockpit_approvals() -> dict:
    """The owner's single triage surface: the real bounded-autonomy gates that need a yes/no. WhatsApp
    drafts awaiting send, bugs (including guest-raised ones), and imported-knowledge decisions (memories
    to remember, entity merges to confirm). Each item carries its own labelled actions. We still never
    invent placeholder approvals; every item here is a genuine pending decision."""
    approvals: list[dict] = []

    # 1) WhatsApp drafts awaiting send
    try:
        from .services.whatsapp import list_drafts
        for d in list_drafts(status="pending"):
            approvals.append({
                "kind": "whatsapp_draft", "label": f"WhatsApp to {d.get('contact','')}",
                "contact": d.get("contact", ""), "body": d.get("body", ""),
                "actions": [
                    {"label": "Approve & send", "style": "go", "url": f"/whatsapp/outbox/{d.get('id')}/approve"},
                    {"label": "Cancel", "style": "no", "url": f"/whatsapp/outbox/{d.get('id')}/cancel"}],
                # legacy contract kept alongside: a stale cached phone PWA reads these
                "approve": f"/whatsapp/outbox/{d.get('id')}/approve",
                "cancel": f"/whatsapp/outbox/{d.get('id')}/cancel"})
    except Exception:
        pass

    # 2) Open bugs (guest-raised ones are flagged so the owner knows a guest hit a problem)
    bug_count = 0
    try:
        from .services.bug_workflow import list_open_bugs
        bugs = list_open_bugs(limit=15)
        bug_count = len(bugs)
        for b in bugs:
            who = "a guest" if b.get("from_guest") else "you"
            tail = "" if b.get("status") == "logged" else f" · {b.get('status')}"
            approvals.append({
                "kind": "bug", "from_guest": bool(b.get("from_guest")),
                "label": f"Bug {b.get('bug_id')} from {who}{tail}", "body": b.get("summary", ""),
                "actions": [
                    {"label": "Diagnose", "style": "go", "url": f"/cockpit/bug/{b.get('bug_id')}/diagnose"},
                    {"label": "Dismiss", "style": "no", "url": f"/cockpit/bug/{b.get('bug_id')}/dismiss"}],
                "approve": f"/cockpit/bug/{b.get('bug_id')}/diagnose",
                "cancel": f"/cockpit/bug/{b.get('bug_id')}/dismiss"})
    except Exception:
        pass

    # 3) Imported-knowledge decisions: memories/insights/claims to approve + entity merges to confirm.
    #    Bounded so a large review queue does not flood the tab; the Knowledge tab holds the full list.
    kn_more = 0
    try:
        from .knowledge_import.store import list_review_queue, list_fusion_suggestions
        review = list_review_queue(status="open", limit=100)
        for it in review[:8]:
            content = _kn_object_summary(it.get("object_type", ""), it.get("object_id", ""))
            nice = {"memory_candidate": "Remember", "insight_candidate": "Insight",
                    "claim": "Claim"}.get(it.get("object_type", ""), "Review")
            sens = " (sensitive)" if it.get("severity") == "high" else ""
            approvals.append({
                "kind": "knowledge_review", "severity": it.get("severity"),
                "label": f"{nice}{sens}", "body": content or it.get("reason", ""),
                "actions": [
                    {"label": "Approve", "style": "go", "url": "/cockpit/knowledge/review/resolve",
                     "body": {"review_id": it.get("review_id"), "action": "approve"}},
                    {"label": "Reject", "style": "no", "url": "/cockpit/knowledge/review/resolve",
                     "body": {"review_id": it.get("review_id"), "action": "reject"}}]})
        kn_more = max(0, len(review) - 8)
        for s in list_fusion_suggestions(status="open", limit=8):
            dup = s.get("duplicate_name") or s.get("duplicate_entity_id")
            approvals.append({
                "kind": "entity_merge",
                "label": f"Merge \"{dup}\" into \"{s.get('canonical_name')}\"",
                "body": f"confidence {round(float(s.get('confidence') or 0), 2)} · {s.get('strategy','')}",
                "actions": [
                    {"label": "Approve merge", "style": "go", "url": "/cockpit/knowledge/fusion/resolve",
                     "body": {"suggestion_id": s.get("suggestion_id"), "action": "approve"}},
                    {"label": "Reject", "style": "no", "url": "/cockpit/knowledge/fusion/resolve",
                     "body": {"suggestion_id": s.get("suggestion_id"), "action": "reject"}}]})
    except Exception:
        pass

    return {"approvals": approvals,
            "counts": {"total": len(approvals), "bugs": bug_count, "knowledge_more": kn_more}}


@app.get("/tools/catalogue")
def tools_catalogue_endpoint() -> dict:
    return {"tools": tool_catalogue()}


class RememberRequest(BaseModel):
    fact: str


@app.get("/memory")
def memory_list() -> dict:
    from .services.memory import all_facts
    return {"facts": all_facts()}


@app.post("/memory")
def memory_add(request: RememberRequest) -> dict:
    from .services.memory import remember_fact
    return remember_fact(request.fact, source="explicit")


@app.delete("/memory")
def memory_delete(request: RememberRequest) -> dict:
    from .services.memory import forget_fact
    return forget_fact(request.fact)


@app.post("/conversation/{conversation_id}/clear")
def conversation_clear(conversation_id: str, request: Request) -> dict:
    """Wipe ALL messages for a conversation_id. DEFAULT_CONVERSATION_ID is shared by every
    surface (cockpit, IRC chat, voice, phone), so clearing it is irreversible and affects
    everyone, not just the caller's own session. Nonce-gated like every other destructive
    action in this app, this previously relied on loopback trust alone, unlike its siblings."""
    _require_cockpit_nonce(request)
    from .services.memory import clear_conversation
    return clear_conversation(conversation_id)


@app.get("/conversation/continuity")
def conversation_continuity(request: Request) -> dict:
    """Read-only context-continuity health: confirms the single owner thread, guest-thread isolation,
    replay budget and message/fact counts without returning message or memory content."""
    _require_cockpit_nonce(request)
    from .services.conversation_continuity import continuity_report
    return continuity_report()


@app.get("/persona")
def persona(mode: str | None = None) -> dict:
    return persona_payload(mode)


class ChatRequest(BaseModel):
    prompt: str
    # "auto" runs the rule-based classifier. Pass an explicit role
    # ("chat"/"tool"/"code") to override and skip classification.
    role: str = "auto"
    system: str | None = None
    model: str | None = None
    # Opt-in: allow escalation to cloud instant models if local can't answer.
    # Off by default so no turn ever reaches a paid API unless asked.
    allow_cloud: bool = False
    # Persona mode for chat-role turns: default/adult/admin/weather. Selects
    # the persona card injected as system when no explicit system is given.
    mode: str | None = None
    # Conversation id for short-term memory. When set, recent history and
    # relevant long-term facts are included, and the exchange is persisted.
    conversation_id: str | None = None
    # Escape hatch for automated health/smoke checks ONLY: skip writing this exchange into the
    # single-brain conversation. Defaults to True (today's behaviour, unchanged) for every real
    # UI surface (cockpit, IRC chat, voice, phone) -- none of them ever set this. Exists because
    # an internal readiness probe was writing its literal test prompt/response directly into the
    # user's permanent conversation history on every install/update run, indistinguishable from
    # something the person actually typed.
    persist: bool = True


# Phrases that suggest the local model could not answer and escalation may help.
_UNCERTAIN = (
    "i don't know", "i do not know", "i'm not sure", "i am not sure",
    "i cannot answer", "i can't answer", "no idea", "unable to",
    "i don't have", "i do not have enough",
)


def _looks_uncertain(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _UNCERTAIN) or len(text.strip()) < 3


def _llm_unavailable(error: str, exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": error,
            "message": "Ollama is not reachable or the selected model is not pulled yet. Run `mongoose doctor`, `ollama list`, and `./scripts/pull_models.sh` on the Mac mini.",
            "reason": str(exc),
        },
    )


@app.post("/llm/chat")
async def llm_chat(request: ChatRequest, http_request: Request) -> dict:
    requested = request.role
    auto_selected = False
    if requested == "auto":
        role = classify_role(request.prompt)
        auto_selected = True
    elif requested in USER_REQUESTABLE_ROLES:
        role = requested
    else:
        # Anything else (including "research"/"code", which are SILENT worker roles)
        # is not a user-facing speaker. Fall back to chat so the worker model can
        # never be the direct reply: the one-speaker coherence rule holds even for
        # API callers. To use the worker, the owner calls the `research` tool.
        role = "chat"
    settings = get_settings()
    if role == "tool":
        # A tool turn plans, validates against the allowlist, executes, and
        # phrases the result. Falls back to a persona reply if no tool fits.
        try:
            outcome = await run_tool_turn(request.prompt)
        except Exception as exc:
            raise _llm_unavailable("local_tool_model_unavailable", exc) from exc
        return {
            "role": role,
            "auto_selected": auto_selected,
            "model": settings.model_for_role("tool"),
            "tool": outcome.get("tool"),
            "executed": outcome.get("executed", False),
            "result": outcome.get("result"),
            "steps": outcome.get("steps", []),
            "stop_reason": outcome.get("stop_reason"),
            "rejected_tool": outcome.get("rejected_tool"),
            "reply": outcome.get("reply"),
        }
    # For chat turns, inject the persona card for the requested mode unless the
    # caller supplied their own system prompt.
    system = request.system
    if role == "chat" and system is None:
        system = persona_card(request.mode)
    remembered: list[str] = []
    try:
        # SINGLE BRAIN: always use DEFAULT_CONVERSATION_ID for chat role turns, regardless of whether
        # the caller provided a conversation_id. This is the single-brain, single-conversation principle:
        # IRC chat, voice, the cockpit browser, and the phone all share one memory and one context.
        # A request about tasks or home from any surface sees the same history and uses the same tools.
        from .services.memory import DEFAULT_CONVERSATION_ID
        # WRITING into any permanent conversation is reserved for owner surfaces. Loopback
        # trust still lets a local process ASK and get an answer, but an anonymous caller can
        # no longer leave anything behind in the owner's (or a guest's) history, the exact
        # mechanism by which an internal health check once wrote fake exchanges into the
        # permanent brain. Owner surfaces (cockpit nonce, master token, the owner's paired
        # phone, a logged-in session) behave exactly as before.
        if _owner_surface(http_request):
            convo_id = request.conversation_id or (
                DEFAULT_CONVERSATION_ID if (role == "chat" and request.persist) else None
            )
        else:
            convo_id = None
        if convo_id:
            mem_out = await chat_with_memory(
                request.prompt,
                conversation_id=convo_id,
                role=role,
                mode=request.mode,
                system=request.system,
            )
            reply = mem_out["reply"]
            remembered = mem_out["remembered"]
        else:
            reply = await ask_ollama(
                request.prompt,
                system=system,
                role=role,
                model=request.model,
            )
    except Exception as exc:
        raise _llm_unavailable("local_model_unavailable", exc) from exc
    source = "local"
    cloud_info = None
    # Escalate only if asked, and only if the local answer looks uncertain.
    if request.allow_cloud and _looks_uncertain(reply):
        budget = TurnBudget(limit_pennies=settings.cloud_turn_budget_pennies)
        try:
            cloud = await escalate(request.prompt, system, budget)
            reply = cloud["reply"]
            source = cloud["provider"]
            cloud_info = {
                "provider": cloud["provider"],
                "model": cloud["model"],
                "est_pennies": cloud["est_pennies"],
                "turn_spent_pennies": round(budget.spent_pennies, 4),
            }
        except BudgetExceeded as exc:
            # Keep the local reply; report why no cloud call happened.
            cloud_info = {"escalated": False, "reason": str(exc)}
    return {
        "role": role,
        "auto_selected": auto_selected,
        "model": request.model or settings.model_for_role(role),
        "source": source,
        "cloud": cloud_info,
        "reply": reply,
        "remembered": remembered,
        "conversation_id": request.conversation_id,
    }


@app.get("/weather/voice-summary")
async def weather_voice_summary(force: bool = False) -> dict:
    return await weather_summary(force=force)


@app.post("/weather/refresh")
async def weather_refresh() -> dict:
    return await weather_summary(force=True)


@app.post("/home/command")
async def home_command(command: HomeCommand) -> dict:
    return await route_home_command(command)


@app.get("/home/routes")
def home_routes(limit: int = 100) -> dict:
    return {"routes": list_route_preferences(limit=limit)}


@app.post("/notifications/ingest")
async def notifications_ingest(item: NotificationIn) -> dict:
    return await ingest_notification(item)


@app.get("/notifications/recent")
def notifications_recent(hours: int = 720, limit: int = 50) -> dict:
    """Browse recent notifications without needing to already know a search term. Search alone
    left notifications completely unbrowsable if you didn't know the exact matching word, even
    when Overview could correctly report one exists. Defaults to 30 days, not the daily brief's
    24h window, since this is for finding something specific, not a daily summary."""
    from .services.notifications import recent_notifications
    return {"notifications": recent_notifications(hours=hours, limit=limit)}


@app.get("/notifications/search")
def notifications_search(q: str = Query(..., min_length=1), limit: int = 10) -> dict:
    return {"results": search_notifications(q, limit)}


@app.get("/ha-prompts")
def ha_prompts_list() -> dict:
    """The menu of named HA assistant prompts (no model call)."""
    from .services.ha_prompts import list_prompts
    return {"prompts": list_prompts()}


@app.post("/notifications/push")
async def notifications_push(body: dict) -> dict:
    """Send a push notification to the user's phone via ntfy."""
    from .services.notifications import send_to_phone
    return await send_to_phone(
        str(body.get("title", "Mongoose")),
        str(body.get("body", "")),
        priority=str(body.get("priority", "default")),
        tags=body.get("tags"),
        click=body.get("click"),
    )


@app.post("/device-files/ingest")
def device_files_ingest(body: dict) -> dict:
    """Ingest a batch of file-index records pushed from another device (the Android
    agent). Body: {files: [{path, filename, text?, mtime?, device_id?}], device?}.
    Writes into the shared file index tagged by device. No file bytes are sent."""
    from .services.file_index import ingest_device_files
    files = body.get("files") or []
    device = str(body.get("device") or "phone")
    if not isinstance(files, list):
        return {"indexed": 0, "skipped": 0, "error": "files must be a list"}
    return ingest_device_files(files, device=device)














@app.get("/cockpit/config-health")
def cockpit_config_health() -> dict:
    """Config-vs-reality reconciliation: configured things that don't match the machine (a role pointing
    at an uninstalled model, a feature enabled without its dependency, a wired-but-off transport), each
    with a concrete fix. The missing check the installer/updater never ran."""
    from .services.config_reconcile import reconcile
    return {"ok": True, **reconcile()}


@app.get("/cockpit/memory/guard")
def cockpit_memory_guard() -> dict:
    """Memory-guard view: pressure level, the protected pinned model, any wasteful secondary model
    resident, and the external hogs (browser/Spotlight/Docker) with their exact reclaim actions."""
    from .services.memory_guard import snapshot
    return {"ok": True, **snapshot()}


@app.post("/cockpit/memory/guard/relieve")
def cockpit_memory_guard_relieve(request: Request) -> dict:
    """Apply the server-SAFE relief: evict any secondary Ollama model, keep the pinned main model.
    External hogs are returned as recommendations (never killed here). Owner-only."""
    _require_cockpit_nonce(request)
    from .services.memory_guard import apply_safe_relief
    return apply_safe_relief()


@app.get("/cockpit/voice/transcriber")
def cockpit_voice_transcriber_status() -> dict:
    """Is local voicemail transcription installed and usable? Plus any in-flight install progress."""
    from .services.model_install import transcriber_status, install_state
    return {"ok": True, "status": transcriber_status(), "install": install_state("transcriber")}


@app.post("/cockpit/voice/transcriber/install")
def cockpit_voice_transcriber_install(request: Request) -> dict:
    """One-click provision of ffmpeg + whisper.cpp + the ggml model. Owner-only (nonce-gated).
    Returns immediately; poll /cockpit/voice/transcriber (or pull-status?model=transcriber)."""
    _require_cockpit_nonce(request)
    from .services.model_install import start_transcriber_install
    return start_transcriber_install()






@app.get("/item/results")
def item_results(result_set_id: str = "") -> dict:
    """Return the latest or named persisted search result set. Read-only; useful after a browser
    refresh or a chat follow-up like 'the first one'."""
    from .services.item_model import result_set
    rs = result_set(result_set_id or None)
    if not rs:
        return {"ok": False, "error": "no stored result set", "items": []}
    return {"ok": True, "result_set": rs}


@app.get("/item/actions")
def item_actions(item_ref: str = "", ordinal: int | None = None, result_set_id: str = "") -> dict:
    """Describe what actions are available for a persisted search result without taking them."""
    from .services.item_model import describe_actions
    return describe_actions(item_ref or None, ordinal=ordinal, result_set_id=result_set_id or None)


@app.post("/whatsapp/webhook/waha")
async def whatsapp_webhook_waha(request: Request) -> dict:
    """Receive inbound WhatsApp events from the WAHA sidecar. The container posts
    from a non-loopback address (host.docker.internal), so this route does its OWN
    verification: a valid WAHA HMAC signature OR a valid X-Mongoose-Token. It never
    accepts an unverified post. Verified messages are normalised and ingested."""
    import json as _json
    from .services.whatsapp_providers.waha import verify_hmac
    raw = await request.body()
    sig = request.headers.get("x-webhook-hmac") or request.headers.get("x-hub-signature-512")
    token = request.headers.get("x-mongoose-token", "")
    settings = get_settings()
    api_token = getattr(settings, "api_token", "") or ""
    token_ok = bool(api_token and token == api_token)
    if not (verify_hmac(raw, sig) or token_ok):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=401, content={"error": "unverified_webhook"})
    try:
        payload = _json.loads(raw or b"{}")
    except Exception:
        return {"ok": False, "error": "bad_json"}
    import asyncio as _asyncio
    from .services.whatsapp import process_webhook
    # OFF the loop: process_webhook does sync SQL ingest and can fetch media over sync httpx (up to 20s)
    # while normalising an inbound message; inline it would stall the loop on every WhatsApp event.
    return await _asyncio.to_thread(process_webhook, payload, provider_name="waha")


@app.get("/whatsapp/status")
def whatsapp_status() -> dict:
    """Session readiness (SCAN_QR_CODE / WORKING / ...), not just 'container up'."""
    from .services.whatsapp_providers.base import get_provider
    p = get_provider()
    return p.status() if p else {"ok": False, "error": "no_provider"}


@app.get("/whatsapp/search")
def whatsapp_search_route(q: str = Query(..., min_length=1), limit: int = 10) -> dict:
    from .services.whatsapp import search_messages
    return {"results": search_messages(q, limit)}


@app.post("/whatsapp/draft")
def whatsapp_draft_route(body: dict) -> dict:
    """Create a pending draft (does NOT send)."""
    from .services.whatsapp import draft_message
    contact = str(body.get("contact", ""))
    text = str(body.get("body", ""))
    if not contact or not text:
        return {"ok": False, "error": "contact and body required"}
    return draft_message(contact, text, chat_id=str(body.get("chat_id", "")))


@app.post("/whatsapp/outbox/{draft_id}/approve")
def whatsapp_approve_route(draft_id: int, request: Request) -> dict:
    """Approve and send a specific draft. This is the human-approval gate. The model never reaches
    it. Accepts either:
      - The cockpit nonce (browser tap): normal cockpit path.
      - The master API token (legacy / direct CLI): still accepted.
      - A draft-scoped one-time approval token (X-Draft-Action-Token header): the ntfy phone
        tap path. Each such token can only approve THIS draft, only once, within its TTL. The
        master API token is NEVER sent to ntfy: this is how we fixed the P0 token leak."""
    from .services.whatsapp import consume_draft_action_token
    draft_token = (request.headers.get("x-draft-action-token") or "").strip()
    if draft_token:
        if not consume_draft_action_token(draft_token, draft_id, "approve"):
            raise HTTPException(status_code=403, detail="invalid, expired, or already-used approval token")
    else:
        _require_token_or_cockpit_nonce(request)
    from .services.whatsapp import approve_and_send
    return approve_and_send(draft_id)


@app.post("/whatsapp/outbox/{draft_id}/cancel")
def whatsapp_cancel_route(draft_id: int, request: Request) -> dict:
    """Cancel a specific draft. Accepts cockpit nonce, master token, or draft-scoped cancel token."""
    from .services.whatsapp import consume_draft_action_token
    draft_token = (request.headers.get("x-draft-action-token") or "").strip()
    if draft_token:
        if not consume_draft_action_token(draft_token, draft_id, "cancel"):
            raise HTTPException(status_code=403, detail="invalid, expired, or already-used cancel token")
    else:
        _require_token_or_cockpit_nonce(request)
    from .services.whatsapp import cancel_draft
    return cancel_draft(draft_id)



# ── RTM Leaseholder Portal endpoints ────────────────────────────────────────
# Spec: docs/RTM_PORTAL_SPEC.md
# These endpoints are NOT public. All require cockpit nonce or API token.
# The portal does not take payments, confirm payments, mark payments as received,
# create ledger entries, or alter bank reconciliation data.










































# ── Two-schedule apportionment (docs/RTM_APPORTIONMENT_SPEC.md) ────────────────



































@app.get("/remote/readiness")
def remote_readiness_route(request: Request, probe: bool = True) -> dict:
    """End-to-end public-access chain check: URL, hardening flag, bind, token, DDNS, DNS,
    certificate, and an outside-in probe. Gated; reveals network posture."""
    _require_token_or_cockpit_nonce(request)
    from .services.remote_access import remote_readiness
    return remote_readiness(probe_outside=probe)




# -------- Knowledge Import & Insight (adopted package: provenance-first, review-gated) --------
@app.post("/cockpit/knowledge/import")
async def cockpit_knowledge_import(request: Request) -> dict:
    """Import a staged dump (asset_id or path). Compiles claims/entities/edges/insights and applies only
    accepted (safe, user-stated, high-confidence) memory; sensitive items stay review-gated."""
    _require_cockpit_nonce(request)
    from .knowledge_import import service as _svc
    from .services import assets as _assets
    payload = await request.json()
    path = payload.get("path")
    if not path and payload.get("asset_id"):
        pth = _assets.asset_path(payload["asset_id"])
        path = str(pth) if pth else None
    if not path:
        return {"ok": False, "error": "provide asset_id or path"}
    try:
        report = _svc.import_bulk_dump(path)
    except Exception as e:
        return {"ok": False, "error": f"import failed: {e}"}
    return report.model_dump() if hasattr(report, "model_dump") else dict(report)


@app.get("/cockpit/knowledge/dashboard")
def cockpit_knowledge_dashboard(request: Request) -> dict:
    _require_cockpit_nonce(request)
    from .knowledge_import.store import dashboard
    return dashboard(limit=int(request.query_params.get("limit", "12")))


@app.get("/cockpit/knowledge/graph")
def cockpit_knowledge_graph(request: Request) -> dict:
    """A readable slice of the entity/claim graph for the cockpit mini-visualisation. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .knowledge_import.store import graph_snapshot
    return {"ok": True, **graph_snapshot(limit=int(request.query_params.get("limit", "48")))}


@app.get("/cockpit/knowledge/care")
def cockpit_knowledge_care(request: Request) -> dict:
    """Care suggestions the owner can review and activate, plus currently active plans. High-risk and
    psychological material never appears here as an auto-candidate (the compiler gates it). Nonce-gated."""
    _require_cockpit_nonce(request)
    from .knowledge_import.care_planner import rank_care_candidates
    from .knowledge_import.store import list_care_plans
    limit = int(request.query_params.get("limit", "6"))
    return {"ok": True,
            "candidates": rank_care_candidates(limit=limit),
            "active": list_care_plans(status="active", limit=20)}


@app.post("/cockpit/knowledge/care/activate")
async def cockpit_knowledge_care_activate(request: Request) -> dict:
    """Owner-initiated activation of a reviewed care suggestion into a gentle, contextual plan. Nonce-gated."""
    _require_cockpit_nonce(request)
    try:
        p = await request.json()
    except Exception:
        return {"ok": False, "error": "insight_id required (send JSON body: {insight_id, level?, cadence?})"}
    insight_id = str(p.get("insight_id") or "")
    if not insight_id:
        return {"ok": False, "error": "insight_id required"}
    from .knowledge_import.care_planner import activate_care_plan
    return activate_care_plan(insight_id, activation_level=str(p.get("level") or "gentle"),
                              cadence=str(p.get("cadence") or "contextual"), actor="owner")


@app.post("/cockpit/knowledge/care/pause")
async def cockpit_knowledge_care_pause(request: Request) -> dict:
    """Pause an active care plan. Nonce-gated."""
    _require_cockpit_nonce(request)
    try:
        p = await request.json()
    except Exception:
        return {"ok": False, "error": "JSON body required: send Content-Type: application/json with a JSON body, or the body is malformed"}
    plan_id = str(p.get("plan_id") or "")
    if not plan_id:
        return {"ok": False, "error": "plan_id required"}
    from .knowledge_import.care_planner import pause_care_plan
    return pause_care_plan(plan_id, actor="owner")


@app.get("/cockpit/knowledge/research/list")
def cockpit_knowledge_research_list(request: Request) -> dict:
    """Recent research missions with their evidence/source cards (snippet-only, review-before-advice).
    Nonce-gated."""
    _require_cockpit_nonce(request)
    import json as _json
    from .knowledge_import.research_broker import list_research_requests
    out = []
    for r in list_research_requests(limit=int(request.query_params.get("limit", "8"))):
        result = {}
        try:
            result = _json.loads(r.get("result_json") or "{}")
        except Exception:
            result = {}
        out.append({
            "request_id": r.get("request_id"), "topic": r.get("topic"), "domain": r.get("domain"),
            "status": r.get("status"), "created_at": r.get("created_at"),
            "evidence": (result.get("evidence") or [])[:6],
        })
    return {"ok": True, "research": out}


@app.get("/cockpit/knowledge/review")
def cockpit_knowledge_review(request: Request) -> dict:
    _require_cockpit_nonce(request)
    from .knowledge_import.store import list_review_queue
    return {"ok": True, "items": list_review_queue(status="open", limit=50)}


@app.post("/cockpit/knowledge/review/resolve")
async def cockpit_knowledge_review_resolve(request: Request) -> dict:
    _require_cockpit_nonce(request)
    from .knowledge_import.store import resolve_review_item
    try:
        p = await request.json()
    except Exception:
        return {"ok": False, "error": "JSON body required: send Content-Type: application/json with a JSON body, or the body is malformed"}
    return resolve_review_item(p.get("review_id", ""), p.get("action", ""), reason=p.get("reason", ""))


@app.get("/cockpit/knowledge/insights")
def cockpit_knowledge_insights(request: Request) -> dict:
    _require_cockpit_nonce(request)
    from .knowledge_import.insight_engine import list_insights
    return {"ok": True, "insights": list_insights(limit=50)}


@app.post("/cockpit/knowledge/insight/feedback")
async def cockpit_knowledge_insight_feedback(request: Request) -> dict:
    _require_cockpit_nonce(request)
    from .knowledge_import.insight_engine import record_feedback
    try:
        p = await request.json()
    except Exception:
        return {"ok": False, "error": "JSON body required: send Content-Type: application/json with a JSON body, or the body is malformed"}
    return record_feedback(p.get("insight_id", ""), p.get("feedback", ""), context=p.get("context"))


@app.post("/cockpit/knowledge/reflect")
async def cockpit_knowledge_reflect(request: Request) -> dict:
    """Run a reflection cycle across imported claims (queues research for sensitive pending items)."""
    _require_cockpit_nonce(request)
    from .knowledge_import.reflection import run_reflection_cycle
    return run_reflection_cycle(kind="manual", queue_research=True)


@app.post("/cockpit/knowledge/reflection/run")
async def cockpit_knowledge_reflection_run(request: Request) -> dict:
    """Alias for the reflection cycle (matches the package's expected route)."""
    _require_cockpit_nonce(request)
    from .knowledge_import.reflection import run_reflection_cycle
    p = {}
    try:
        p = await request.json()
    except Exception:
        pass
    return run_reflection_cycle(kind=p.get("kind", "manual"), queue_research=True)


@app.get("/cockpit/knowledge/search")
def cockpit_knowledge_search(request: Request) -> dict:
    """Search imported knowledge (claims, insights, memories, persona rules, entities, care plans)."""
    _require_cockpit_nonce(request)
    from .knowledge_import.store import search_knowledge
    q = request.query_params.get("q", "") or request.query_params.get("query", "")
    return {"ok": True, "results": search_knowledge(q, limit=int(request.query_params.get("limit", "20")))}


@app.post("/cockpit/knowledge/research")
async def cockpit_knowledge_research(request: Request) -> dict:
    """Brokered research via the existing SearXNG client. Degrades honestly if web search is off."""
    _require_cockpit_nonce(request)
    from .knowledge_import.research_broker import run_research_request, ResearchRequest
    p = await request.json()
    req = ResearchRequest(topic=p.get("topic", ""), question=p.get("question", p.get("topic", "")),
                          domain=p.get("domain", "general"), why_needed=p.get("why_needed", "owner asked"),
                          source_policy=p.get("source_policy", "broad_web"),
                          time_sensitivity=p.get("time_sensitivity", "stable"))
    return await run_research_request(req, insight_id=p.get("insight_id"))


@app.post("/cockpit/knowledge/fusion/propose")
def cockpit_knowledge_fusion_propose(request: Request, body: dict = Body(default={})) -> dict:
    """Create reviewable entity-fusion suggestions (deterministic fuzzy + optional local model). Nothing
    is merged until approved. Sync so the opt-in LLM path can run in the worker thread."""
    _require_cockpit_nonce(request)
    from .knowledge_import.entity_suggestions import suggest_entity_fusions
    return suggest_entity_fusions(limit=int((body or {}).get("limit") or 60),
                                  use_llm=bool((body or {}).get("use_llm", False)))


@app.post("/cockpit/knowledge/entity/suggest-fusions")
def cockpit_knowledge_entity_suggest_fusions(request: Request, body: dict = Body(default={})) -> dict:
    """Alias of fusion/propose (entity-centric path)."""
    _require_cockpit_nonce(request)
    from .knowledge_import.entity_suggestions import suggest_entity_fusions
    return suggest_entity_fusions(limit=int((body or {}).get("limit") or 60),
                                  use_llm=bool((body or {}).get("use_llm", False)))


@app.get("/cockpit/knowledge/fusion/proposals")
def cockpit_knowledge_fusion_proposals(request: Request) -> dict:
    _require_cockpit_nonce(request)
    from .knowledge_import.store import list_entity_fusions, list_fusion_suggestions
    return {"ok": True, "active": list_entity_fusions(limit=50), "suggestions": list_fusion_suggestions(limit=50)}


@app.post("/cockpit/knowledge/fusion/resolve")
def cockpit_knowledge_fusion_resolve(request: Request, body: dict = Body(default={})) -> dict:
    """Approve/reject a suggested merge. Approval is the only thing that writes an active fusion + edges."""
    _require_cockpit_nonce(request)
    from .knowledge_import.store import resolve_fusion_suggestion
    return resolve_fusion_suggestion(str((body or {}).get("suggestion_id") or ""),
                                     str((body or {}).get("action") or "keep_open"))


@app.post("/cockpit/knowledge/synthesis/run")
def cockpit_knowledge_synthesis_run(request: Request, body: dict = Body(default={})) -> dict:
    """Run the multi-claim synthesis cycle (optional local model). Cards are reviewable, not auto-promoted."""
    _require_cockpit_nonce(request)
    from .knowledge_import.synthesis import run_synthesis_cycle
    return run_synthesis_cycle(use_llm=bool((body or {}).get("use_llm", False)),
                               limit=int((body or {}).get("limit") or 30))


@app.get("/cockpit/knowledge/synthesis")
def cockpit_knowledge_synthesis_list(request: Request) -> dict:
    """List synthesis cards (cross-claim summaries), reviewable but never auto-promoted to advice. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .knowledge_import.store import list_synthesis_cards
    return {"ok": True, "cards": list_synthesis_cards(limit=int(request.query_params.get("limit", "20")))}








































































@app.get("/body/data/status")
def body_data_status_route() -> dict:
    """What training/nutrition data Mongoose has, and the current upload request (if any). Read-only;
    used by the cockpit Body data card and the phone."""
    from .services.body_data_nudge import nudge_status
    return nudge_status()


@app.post("/cockpit/chat/attach")
async def cockpit_chat_attach_route(request: Request) -> dict:
    """A file attached to a chat message from the browser (<= 30MB). Body-data CSVs are imported;
    everything else is stored as a referenceable asset. Owner-gated (cockpit nonce or master token).

    Raw body with X-Filename, or multipart 'file'. Returns the lane (body_data / asset) and a summary
    the chat composer shows inline."""
    _require_token_or_cockpit_nonce(request)
    from .services.uploads import handle_chat_attachment
    filename, data, mime = await _read_upload(request)
    if data is None:
        return {"ok": False, "error": "no file received; attach a file"}
    return handle_chat_attachment(filename, data, uploaded_by="owner", surface="cockpit-chat",
                                  mime_type=mime)








@app.post("/body/data/ingest")
async def body_data_ingest_route(request: Request) -> dict:
    """Import an Alpha Progression (sessions/sets/body-comp) or MyFitnessPal CSV from the browser.

    Accepts either JSON {filename, text} or a raw body with X-Filename. Auto-detects the kind, imports
    it, and silences the data nudge on success. Owner-gated (cockpit nonce or master token)."""
    _require_token_or_cockpit_nonce(request)
    from .services.body_data_nudge import ingest_csv
    filename = ""
    text = ""
    ctype = request.headers.get("content-type", "") or ""
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except Exception:
            return {"ok": False, "error": "expected a JSON body with filename and text"}
        filename = str((payload or {}).get("filename") or "")
        text = str((payload or {}).get("text") or "")
    else:
        filename = request.headers.get("x-filename", "") or request.query_params.get("filename", "")
        raw = await request.body()
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = ""
    return ingest_csv(filename, text)






















# --- RTM receipts + turnstile trust reconciliation (none public; nonce/token gated) ---
# Every endpoint here records and checks only. None initiates, confirms, or simulates a bank instruction.













@app.post("/ha-prompt/{key}")
async def ha_prompt_run(key: str, body: dict | None = None) -> dict:
    """Run a named HA prompt through Mongoose's brain. Optional JSON body {extra}."""
    from .services.ha_prompts import run_prompt
    extra = (body or {}).get("extra", "") if isinstance(body, dict) else ""
    return await run_prompt(key, extra=str(extra))


@app.post("/calendar/events")
def calendar_add_event(event: EventIn) -> dict:
    return add_event(event)


@app.post("/calendar/tasks")
def calendar_add_task(task: TaskIn) -> dict:
    return add_task(task)


@app.get("/calendar/availability")
def calendar_availability(start_at: str, end_at: str) -> dict:
    return availability(start_at, end_at)


@app.get("/calendar/today")
def calendar_today() -> dict:
    return {"events": events_today()}


@app.get("/tasks/today")
def tasks_today_endpoint(include_open_without_due: bool = False) -> dict:
    return {"tasks": tasks_today(include_open_without_due=include_open_without_due)}


@app.get("/cockpit/tasks")
def cockpit_tasks_list(request: Request, scope: str = "all", include_completed: bool = False,
                       limit: int = 200) -> dict:
    """The full task list for the Tasks screen, with the RTM/personal/unclassified split.

    `direction` is returned so the screen can state the sync boundary honestly instead of leaving the
    owner to assume it: Microsoft To Do is imported INTO Mongoose and nothing is ever written back
    (the connector holds Tasks.Read and has no write path), so an RTM task cannot reach Microsoft.
    """
    _require_cockpit_nonce(request)
    from .services.calendar_tasks import list_tasks, task_counts
    rows = list_tasks(limit=limit, include_completed=include_completed, scope=scope)
    # Strip the raw source payloads. They were nearly half the response and the screen never reads them;
    # on a phone over the tunnel that is dead weight on every refresh.
    slim = [{k: v for k, v in r.items() if k not in ("raw_json", "raw_rfc822_b64")} for r in rows]
    return {"ok": True, "tasks": slim, "counts": task_counts(rows), "scope": scope,
            "direction": "microsoft_to_mongoose_only"}


@app.post("/cockpit/tasks/classify-suggest")
def cockpit_tasks_classify_suggest(request: Request) -> dict:
    """Advisory RTM guesses for every UNCLASSIFIED task, so the owner can review a proposed split in
    one pass instead of ticking 77 boxes. Returns suggestions ONLY , it never writes a classification,
    because a silently wrong label on company work is worse than an unset one."""
    _require_cockpit_nonce(request)
    from .services.calendar_tasks import list_tasks, guess_is_rtm
    out = [{"id": t["id"], "title": t.get("title", ""), "suggest_rtm": guess_is_rtm(t.get("title", ""))}
           for t in list_tasks(limit=500, scope="unclassified")]
    return {"ok": True, "suggestions": out, "count": len(out),
            "note": "advisory only, nothing has been changed"}


@app.post("/cockpit/tasks/{task_id}")
async def cockpit_tasks_update(task_id: int, request: Request) -> dict:
    """Edit one task: due date, priority, status, RTM flag, reminder. Local-only by design."""
    _require_cockpit_nonce(request)
    from .services.calendar_tasks import update_task
    body = await request.json()
    clear = tuple(str(c) for c in (body.get("clear") or []))
    return update_task(
        task_id,
        due_at=body.get("due_at"), reminder_at=body.get("reminder_at"),
        priority=body.get("priority"), status=body.get("status"),
        is_rtm=(None if body.get("is_rtm") is None else bool(body.get("is_rtm"))),
        clear=clear)






@app.post("/cockpit/mail/refresh")
async def cockpit_mail_refresh(request: Request) -> dict:
    """Sync the IMAP INBOX and Sent folders on demand, so a just-sent or just-received email shows in RTM
    Mail without waiting for the background cadence. Nonce-gated, off-thread, read-only toward the server."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio

    def _do() -> dict:
        from .services.crawler import crawl_imap_fresh
        out: dict = {"ok": True}
        for folder in ("INBOX", "Sent"):
            try:
                out[folder.lower()] = crawl_imap_fresh(folder)
            except Exception as exc:
                out[folder.lower()] = {"ok": False, "error": str(exc)[:200]}
        return out

    return await _asyncio.to_thread(_do)










@app.get("/dump/{dump_id}/download")
def dump_download(dump_id: str, request: Request, token: str = "") -> Response:
    """Download a previously created data dump by its id. Two ways in: a valid, unexpired download
    token (the shareable-link form, usable from a browser or another device), or a cockpit nonce (the
    in-app form). With a token we validate it in constant time before serving; without one we require
    the nonce. The dump itself persists; only the link token expires."""
    from .services import data_dump as _dd
    if token:
        if not _dd.validate_download_token(dump_id, token):
            raise HTTPException(status_code=403, detail="this download link is invalid or has expired")
    else:
        _require_cockpit_nonce(request)
    got = _dd.dump_bytes(dump_id)
    if not got:
        raise HTTPException(status_code=404, detail="no dump with that id (it may have been cleaned up)")
    data, filename = got
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{_safe_download_name(filename)}"'})


@app.post("/cockpit/dump/create")
def dump_create(request: Request, body: dict = Body(default={})) -> dict:
    """Create a data dump of the requested kind and return its id + download link. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services import data_dump as _dd
    return _dd.create_dump((body or {}).get("kind", "everything"))


@app.post("/cockpit/dump/redeem")
def dump_redeem(request: Request, body: dict = Body(default={})) -> dict:
    """Redeem a dump id pasted into the Build/Staging area: returns its metadata + the download link so
    the user can pull it here. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services import data_dump as _dd
    rec = _dd.get_dump((body or {}).get("dump_id", ""))
    if not rec:
        return {"ok": False, "error": "no dump with that id"}
    link = _dd.refresh_download_token(rec["dump_id"]) or {}
    return {"ok": True, "dump_id": rec["dump_id"], "kind": rec["kind"], "filename": rec["filename"],
            "size_bytes": rec["size_bytes"], "row_counts": rec.get("row_counts", {}),
            "download_url": link.get("download_url") or f"/dump/{rec['dump_id']}/download",
            "expires_at": link.get("expires_at", "")}


@app.post("/cockpit/dump/list")
def dump_list(request: Request) -> dict:
    """List recent data dumps. Nonce-gated."""
    _require_cockpit_nonce(request)
    from .services import data_dump as _dd
    out = _dd.list_dumps()
    try:
        out["kinds"] = _dd.available_kinds()
    except Exception:
        out["kinds"] = ["everything"]
    return out









class BusSend(BaseModel):
    sender: str
    to: str
    subject: str
    body: str = ""
    type: str = "note"
    re_id: str | None = None
    attachments: list[str] | None = None


@app.post("/bus/send")
def bus_send(req: BusSend) -> dict:
    from .services import bus
    return bus.send_message(sender=req.sender, to=req.to, subject=req.subject,
                            body=req.body, type=req.type, re_id=req.re_id,
                            attachments=req.attachments)


@app.get("/bus/list")
def bus_list(to: str | None = None, sender: str | None = None,
             type: str | None = None, unread_only: bool = True, limit: int = 50) -> dict:
    from .services import bus
    return {"messages": bus.list_messages(to=to, sender=sender, type=type,
                                          unread_only=unread_only, limit=limit)}


@app.get("/bus/read/{message_id}")
def bus_read(message_id: str, archive: bool = True) -> dict:
    from .services import bus
    m = bus.read_message(message_id, archive=archive)
    if m is None:
        raise HTTPException(status_code=404, detail="no such message")
    return m


@app.get("/bus/search")
def bus_search(q: str = Query(..., min_length=1), limit: int = 20) -> dict:
    from .services import bus
    return {"messages": bus.search_messages(q, limit)}


class AssistRequest(BaseModel):
    text: str
    conversation_id: str | None = None
    mode: str = "default"


@app.post("/assist")
async def assist(req: AssistRequest, http_request: Request) -> dict:
    """Single entry point for Home Assistant Assist (or any voice surface).

    Routes to a tool turn when the text asks for home control, otherwise to a
    persona+memory chat turn. Always returns `speech` (what to say back) and a
    `controlled` flag. Keeps all intelligence in Mongoose so the HA component
    stays thin.
    """
    from .tools import _user_asked_for_home_control
    text = (req.text or "").strip()
    if not text:
        return {"speech": "I didn't catch that.", "controlled": False}
    if _user_asked_for_home_control(text):
        try:
            outcome = await run_tool_turn(text)
        except Exception as exc:
            raise _llm_unavailable("local_tool_model_unavailable", exc) from exc
        return {
            "speech": outcome.get("reply") or "Done.",
            "controlled": bool(outcome.get("executed")),
            "tool": outcome.get("tool"),
            "stop_reason": outcome.get("stop_reason"),
        }
    # Conversational: persona + memory, on the ONE permanent Mongoose thread, so what you say to
    # Home Assistant continues the same conversation as the Chat tab, phone and terminal.
    from .services.memory import DEFAULT_CONVERSATION_ID
    # Same owner gate as /llm/chat: HA's integration authenticates with the master token to
    # get through the LAN guard at all, so its continuity is unchanged; what this closes is an
    # anonymous loopback caller quietly writing into the permanent shared history.
    conv = (req.conversation_id or DEFAULT_CONVERSATION_ID) if _owner_surface(http_request) else None
    try:
        if conv:
            out = await chat_with_memory(text, conversation_id=conv, role="chat", mode=req.mode)
        else:
            # Anonymous caller: answer, but write nothing into anyone's permanent history.
            from .persona import persona_card
            reply = await ask_ollama(text, system=persona_card(req.mode), role="chat")
            out = {"reply": reply, "remembered": []}
    except Exception as exc:
        # Assist is a voice surface: a model-down 503 bubbling through to HA causes an
        # unhandled error on the HA side. Return a speech response the user can hear instead.
        import logging
        logging.getLogger("mongoose.assist").warning("assist model unavailable: %s", exc)
        return {"speech": "The local brain isn't responding right now.",
                "controlled": False, "remembered": [], "conversation_id": conv,
                "error": "local_model_unavailable"}
    return {"speech": out["reply"], "controlled": False,
            "remembered": out.get("remembered", []), "conversation_id": conv}


@app.get("/brief")
async def brief(post: bool = False) -> dict:
    from .services.brief import generate_brief
    return await generate_brief(post_to_bus=post)
