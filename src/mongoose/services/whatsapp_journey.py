"""WhatsApp connection journey.

Role-aware WAHA journey. Mongoose can run two WAHA sessions/accounts:

    owner_read     The owner's personal WhatsApp session, used for reading/indexing messages.
    mongoose_send  Mongoose's own WhatsApp number, used to send notifications as Mongoose.

Single-session installs remain backward compatible: if only WHATSAPP_SESSION exists, both roles use it.
"""
from __future__ import annotations

from typing import Any

from .whatsapp_accounts import MONGOOSE_SEND, OWNER_READ, account_for_role, accounts, owner_number


def _provider(role: str | None = None):
    from .whatsapp_providers.base import get_provider
    return get_provider(role=role)


def _provider_safe(role: str | None = None):
    """Tests historically monkeypatch _provider as a zero-arg callable. Keep that seam."""
    try:
        return _provider(role)
    except TypeError:
        return _provider()


def start_container() -> dict[str, Any]:
    """Start the WAHA Docker sidecar (docker compose -f docker-compose.whatsapp.yml up -d). Best-effort:
    reports honestly if Docker is missing or not running, so the cockpit can guide the user instead of
    silently doing nothing. This closes the 'you must run docker compose by hand' gap in the journey."""
    import shutil
    import subprocess
    from pathlib import Path
    if shutil.which("docker") is None:
        return {"ok": False, "detail": "Docker isn't installed. Install Docker Desktop, then try again.",
                "needs": "docker"}
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return {"ok": False, "detail": "Docker is installed but not running. Start Docker, then try again.",
                "needs": "docker_running"}
    compose = Path("docker-compose.whatsapp.yml")
    if not compose.exists():
        return {"ok": False, "detail": "docker-compose.whatsapp.yml is missing from the Mongoose folder."}
    # Source the SAME canonical env the server reads (~/.mongoose/.env) so the container and Mongoose
    # agree on WAHA_API_KEY and WHATSAPP_HOOK_HMAC_KEY. Without --env-file, compose only sees the shell
    # or a folder-local .env, so the webhook HMAC key arrives empty and inbound messages 401 as
    # unverified_webhook even though linking works. Reading one file removes that whole mismatch class.
    cmd = ["docker", "compose"]
    try:
        from ..setup_wizard import ENV_PATH as _canon_env
        if _canon_env.exists():
            cmd += ["--env-file", str(_canon_env)]
    except Exception:
        pass
    cmd += ["-f", str(compose), "up", "-d"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    combined = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    # `docker compose up -d` can exit 0 EVEN WHEN the image failed to pull (e.g. no arm64 manifest on
    # Apple Silicon), leaving no container. So VERIFY the container is actually running rather than
    # trusting the exit code , that false success is exactly why "Start" looked like it did nothing.
    running = subprocess.run(
        ["docker", "ps", "--filter", "name=mongoose-waha", "--filter", "status=running", "-q"],
        capture_output=True, text=True).stdout.strip()
    if running:
        return {"ok": True,
                "detail": "WhatsApp service is starting. Give it about 20 seconds to boot, then Connect "
                          "each account and scan its QR."}
    low = combined.lower()
    if "manifest" in low or "no match for platform" in low or "arm64" in low:
        return {"ok": False, "needs": "platform",
                "detail": "The WhatsApp image had no build for this Mac's chip (arm64). The compose file "
                          "is now set to run it under emulation. Press Start again to pull it."}
    return {"ok": False,
            "detail": (combined or "docker compose ran but no WhatsApp container came up.")[:300]}


def _waha_configured() -> bool:
    """WAHA is usable if an explicit URL is set OR the default local container is actually answering.
    Recognising the running container means pressing Start (which brings that container up) is ENOUGH:
    no WAHA_URL env var to set by hand. Without this, Start worked but Connect/QR stayed blocked because
    this returned False, so the flow could never progress: a dead end after Start."""
    import os
    if os.getenv("WAHA_URL") or os.getenv("WHATSAPP_WAHA_URL"):
        return True
    try:
        import httpx
        from .whatsapp_providers.waha import _waha_url
        # 200 (open) or 401 (needs api key) both mean WAHA is up; only a refused/absent server is "no".
        return httpx.get(_waha_url() + "/api/sessions", timeout=1.5).status_code < 500
    except Exception:
        return False


def _map_state(st: dict[str, Any], *, role: str) -> dict[str, Any]:
    account = account_for_role(role)
    if not st.get("ok"):
        if not _waha_configured():
            return {"state": "not_configured",
                    "detail": "WhatsApp uses a local WAHA container. Start it below, no terminal needed.",
                    "can_connect": False, "can_start_container": True, "role": role, "account": account.__dict__}
        return {"state": "error", "detail": st.get("error", "WAHA unreachable"), "can_connect": False,
                "url": st.get("url"), "role": role, "account": account.__dict__}
    raw = str(st.get("state", "")).upper()
    base = {"role": role, "session": st.get("session") or account.session, "account": account.__dict__,
            "raw_state": raw}
    if raw in ("WORKING", "CONNECTED"):
        return {**base, "state": "connected", "detail": f"{account.label} is connected.", "can_connect": False}
    if raw in ("SCAN_QR_CODE", "STARTING"):
        return {**base, "state": "scan_qr", "detail": f"Scan the QR for {account.label}.",
                "can_connect": False}
    if raw in ("PAIRING", "CONNECTING"):
        return {**base, "state": "connecting", "detail": f"Pairing {account.label}…", "can_connect": False}
    return {**base, "state": "disconnected", "detail": f"{account.label} isn't connected yet.",
            "can_connect": True}


def connection_state(role: str = OWNER_READ) -> dict[str, Any]:
    """State for one WAHA role/session. Defaults to the owner/read account for compatibility."""
    prov = _provider_safe(role)
    if prov is None:
        return {"state": "not_configured", "detail": "No WhatsApp provider available.", "can_connect": False,
                "role": role, "account": account_for_role(role).__dict__}
    return _map_state(prov.status(), role=role)


def accounts_state() -> dict[str, Any]:
    """State for both configured roles so cockpit can guide two QR links."""
    out = []
    for account in accounts():
        # De-duplicate same-session legacy mode while still reporting both purposes.
        st = connection_state(account.role)
        out.append(st)
    from .whatsapp_accounts import summary
    return {"ok": True, **summary(), "roles": out}


def connect(role: str = OWNER_READ) -> dict[str, Any]:
    """Start one role's WAHA session so it begins authenticating."""
    if not _waha_configured():
        return {"ok": False, "error": "WhatsApp isn't started yet. Use the Start WhatsApp "
                "button, no terminal needed.", "role": role, "can_start_container": True}
    prov = _provider_safe(role)
    if prov is None or not hasattr(prov, "start_session"):
        return {"ok": False, "error": "provider can't start a session", "role": role}
    res = prov.start_session()
    if isinstance(res, dict):
        res.setdefault("role", role)
        res.setdefault("session", account_for_role(role).session)
    return res


def login_qr(role: str = OWNER_READ) -> dict[str, Any]:
    """The QR to scan for one role/session, as base64 PNG."""
    # FIX (external audit, additional findings #8): connect() checks _waha_configured()
    # first and gives the right setup instruction; this didn't, so a caller who checked
    # connection_state() (not_configured, with the setup command) and then tried the QR
    # step anyway got a raw "Connection refused" instead of the same clear instruction.
    if not _waha_configured():
        return {"ok": False, "error": "WhatsApp isn't started yet. Use the Start WhatsApp "
                "button, no terminal needed.", "role": role, "can_start_container": True}
    prov = _provider_safe(role)
    if prov is None or not hasattr(prov, "qr"):
        return {"ok": False, "error": "provider has no QR", "role": role}
    res = prov.qr()
    if isinstance(res, dict):
        res.setdefault("role", role)
        res.setdefault("session", account_for_role(role).session)
    return res


def recent_activity(limit: int = 15) -> dict[str, Any]:
    """Recent owner-read WhatsApp messages plus pending drafts and two-account state."""
    from .whatsapp import list_drafts, search_messages
    try:
        recent = search_messages("", limit=limit) if _waha_configured() else []
    except Exception:
        recent = []
    try:
        drafts = list_drafts("pending")
    except Exception:
        drafts = []
    return {"ok": True, "messages": recent, "pending_drafts": drafts, "accounts": accounts_state()}


def send_test(to_number: str = "") -> dict[str, Any]:
    """Queue/send a test WhatsApp notification from Mongoose's own sender session to the owner.

    This proves the notification account works. It still uses the approved-outbox transport boundary;
    the test command performs the approval step because it is an explicit test action.
    """
    state = connection_state(MONGOOSE_SEND)
    if state["state"] != "connected":
        return {"ok": False, "error": f"Mongoose sender WhatsApp isn't connected (state: {state['state']}). Connect the Mongoose notifications account first.",
                "role": MONGOOSE_SEND}
    import os
    to = (to_number or owner_number() or os.getenv("WHATSAPP_SELF_NUMBER", "")).strip()
    if not to:
        return {"ok": False, "error": "No owner/test number. Pass --to or set WHATSAPP_OWNER_NUMBER / WHATSAPP_SELF_NUMBER.",
                "role": MONGOOSE_SEND}
    from .whatsapp import approve_and_send, draft_message
    chat_id = to if "@" in to else f"{to}@c.us"
    d = draft_message(contact=to, body="✅ Mongoose test message - sent from the Mongoose WhatsApp number.",
                      chat_id=chat_id, send_role=MONGOOSE_SEND)
    draft_id = d.get("id")
    if not draft_id:
        return {"ok": False, "error": "couldn't draft test message", "role": MONGOOSE_SEND}
    return approve_and_send(draft_id)
