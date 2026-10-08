from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .settings import get_settings

# Google OAuth uses a LOOPBACK redirect. The redirect URI must be registered in the Google Cloud
# Console, and Google matches it exactly , INCLUDING the port. run_local_server(port=0) picks a RANDOM
# free port every time (e.g. localhost:62334, then 62345...), so the redirect URI can never be
# pre-registered and Google returns "Error 400: redirect_uri_mismatch" on every attempt. The fix is a
# FIXED-by-default port, so the redirect URI is stable and registerable ONCE.
# It remains overrideable because no localhost port can be guaranteed free on every Mac.
DEFAULT_GOOGLE_OAUTH_REDIRECT_PORT = 8766


def google_oauth_redirect_port() -> int:
    """Loopback OAuth callback port. Defaults to 8766, override with GOOGLE_OAUTH_REDIRECT_PORT."""
    raw = os.getenv("GOOGLE_OAUTH_REDIRECT_PORT", str(DEFAULT_GOOGLE_OAUTH_REDIRECT_PORT))
    try:
        port = int(str(raw).strip())
        if 1024 <= port <= 65535:
            return port
    except Exception:
        pass
    return DEFAULT_GOOGLE_OAUTH_REDIRECT_PORT


def _port_is_free(port: int) -> bool:
    """Can we bind this loopback port right now?

    Deliberately does NOT set SO_REUSEADDR. google-auth's run_local_server binds a plain socket, so if
    we probed with SO_REUSEADDR we could bind a port still in TIME_WAIT and report 'free' when the real
    bind would then fail with EADDRINUSE. That false-positive was one half of the port-drift bug (the
    probe said free, the flow failed, and the code drifted to another port). Probing exactly the way the
    real server binds keeps the two in agreement."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _describe_port_holder(port: int) -> str:
    """Best-effort description of what is holding a loopback port, so a 'port busy' error is actionable
    instead of mysterious. On macOS/Linux this shells out to lsof; if that is unavailable it returns a
    generic but still useful instruction. Never raises."""
    generic = (f"run  lsof -nP -iTCP:{port} -sTCP:LISTEN  to see what holds it, then close that "
               f"process (often a leftover Mongoose sign-in server: kill its PID).")
    try:
        import subprocess
        r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                           capture_output=True, text=True, timeout=4)
        out = (r.stdout or "").strip()
        if not out:
            return generic
        lines = out.splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 2:
                cmd, pid = parts[0], parts[1]
                return (f"port {port} is held by '{cmd}' (PID {pid}). Close it with  kill {pid}  "
                        f"(or  kill -9 {pid}  if it does not stop), then try again.")
        return generic
    except Exception:
        return generic


def google_oauth_redirect_uri() -> str:
    """The exact redirect URI to register in the Google Cloud Console (Authorized redirect URIs)."""
    return f"http://localhost:{google_oauth_redirect_port()}/"


def run_google_consent(flow) -> Any:
    """Run an InstalledAppFlow on the user's REGISTERED loopback port. Google matches the redirect URI
    exactly, including the port, so the port the user registered in the Cloud Console is a FIXED target:
    it must not move on its own. If that port is busy, the right answer is to free it (or deliberately
    change it once), NOT to silently drift to another port, drifting is what produced the endless
    8766 -> 8767 -> 8768 climb where every attempt told the user to register a different number.

    So: use the registered port. If it is free, run. If it is busy, stop with a clear, actionable
    message and DO NOT persist a different port. The registered URI stays stable across attempts."""
    port = google_oauth_redirect_port()
    uri = f"http://localhost:{port}/"
    if not _port_is_free(port):
        # Identify what is holding it so the user can act, but never move the registered target.
        holder = _describe_port_holder(port)
        raise RuntimeError(
            f"Google sign-in port {port} is busy right now, so the sign-in server can't start. "
            f"This does NOT mean you registered the wrong port, {uri} is still correct. "
            f"Free the port and try again: {holder} "
            f"If you would rather use a different port permanently, set GOOGLE_OAUTH_REDIRECT_PORT in "
            f".env to a free port and register http://localhost:THAT_PORT/ in the Cloud Console once. "
            f"Mongoose will not change the port on its own."
        )
    try:
        try:
            return flow.run_local_server(port=port, timeout_seconds=180)
        except TypeError:
            return flow.run_local_server(port=port)
    except OSError as exc:
        import errno as _errno
        if exc.errno == _errno.EADDRINUSE:
            # Raced: free at probe, taken at bind. Report it honestly against the SAME registered port;
            # do not drift or persist a new one.
            holder = _describe_port_holder(port)
            raise RuntimeError(
                f"Google sign-in port {port} became busy between the check and starting the server. "
                f"Your registered URI {uri} is still correct, just free the port and try again: {holder}"
            ) from exc
        raise
    except Exception as exc:
        msg = str(exc)
        if "redirect_uri_mismatch" in msg or "redirect_uri" in msg.lower():
            raise RuntimeError(
                "Google rejected the sign-in with redirect_uri_mismatch. In the Google Cloud Console, "
                "open your OAuth client and add this EXACT Authorized redirect URI: "
                f"{google_oauth_redirect_uri()} (then wait ~1 minute and try again)."
            ) from exc
        raise

# Explicit OAuth consent handoff for the setup wizard. These trigger the same
# flows the connectors use lazily on first sync, but on demand during setup so
# the browser/device consent happens now and the token is captured up front.
# Each returns (ok, message) and never raises into the caller.


def authorize_google(scopes: list[str] | None = None) -> tuple[bool, str]:
    """Run Google's installed-app OAuth flow (opens a browser), store the token.

    Requires the google client-secrets JSON to be configured. Uses the union of
    calendar/tasks/gmail read scopes so one consent covers all Google features.
    """
    settings = get_settings()
    secrets_path = Path(settings.google_client_secrets_path)
    if not secrets_path.exists():
        return False, f"Google client secrets not found at {secrets_path}. Add the OAuth JSON first."
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except Exception as exc:
        return False, f"Google auth libraries not installed: {exc}"
    scopes = scopes or [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/tasks.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), scopes)
        creds = run_google_consent(flow)
        token_path = Path(settings.google_token_path)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return True, f"Google authorised; token saved to {token_path}."
    except Exception as exc:
        return False, f"Google authorisation failed: {exc}"


def authorize_microsoft(todo_only: bool = False) -> tuple[bool, str]:
    """Run Microsoft device-code flow and capture the token. Reuses the connector's single auth path
    (one definition of "how we get a Microsoft token"), so it works WITHOUT a registered app, the
    connector falls back to Microsoft's well-known public client and the 'consumers' authority for
    personal accounts. Pass todo_only=True to request just Tasks.Read (minimal consent for To Do)."""
    try:
        from .connectors.microsoft_graph import _token, TODO_ONLY_SCOPES, SCOPES, MicrosoftAuthError
    except Exception as exc:
        return False, f"Microsoft connector unavailable: {exc}"
    try:
        # _token prints the device-code message and blocks until the user signs in, then caches the
        # token (and refresh token) for silent reuse.
        _token(allow_interactive=True, scopes=TODO_ONLY_SCOPES if todo_only else SCOPES)
        return True, ("Microsoft To Do authorised; token cached." if todo_only
                      else "Microsoft authorised; token cached.")
    except MicrosoftAuthError as exc:
        return False, f"Microsoft authorisation failed: {exc}"
    except Exception as exc:
        return False, f"Microsoft authorisation failed: {exc}"


def _candidate_token_paths(primary: str, legacy: list[str]) -> list[str]:
    paths = list(legacy)
    if primary:
        paths.insert(0, primary)
    return paths


def _plausible_token_file(path) -> bool:
    """A token file counts only if it plausibly IS one. A zero-byte or tiny placeholder (an
    interrupted write, a touch'd file, an empty {}) must not read as 'signed in': that false
    positive triggers real sync attempts against Graph/Gmail that then fail confusingly. Any
    genuine OAuth token cache is far larger than this threshold."""
    try:
        q = Path(path).expanduser()
        return q.exists() and q.stat().st_size > 64
    except Exception:
        return False


def google_token_present() -> bool:
    """The single source of truth for 'is Google actually signed in'. Checks the configured
    path plus known legacy relative locations, so every UI surface that asks this question
    gets the same answer -- previously two independent checks (one path-complete, one not)
    could and did disagree on this exact fact."""
    settings = get_settings()
    for p in _candidate_token_paths(getattr(settings, "google_token_path", ""), [
        "./secrets/google-token.json", "./secrets/gmail-token.json",
        "./secrets/google-oauth-token.json", "./secrets/token.json",
    ]):
        try:
            if p and _plausible_token_file(p):
                return True
        except Exception:
            continue
    return False


def microsoft_token_present() -> bool:
    """The single source of truth for 'is Microsoft actually signed in'. See
    google_token_present for why this consolidation exists."""
    settings = get_settings()
    for p in _candidate_token_paths(getattr(settings, "ms_token_cache", ""), [
        "./secrets/msal-token-cache.bin", "./secrets/ms-token-cache.json",
        "./secrets/msal-token-cache.json", "./secrets/microsoft-token.json",
    ]):
        try:
            if p and _plausible_token_file(p):
                return True
        except Exception:
            continue
    return False


def oauth_status() -> dict[str, Any]:
    """Report whether each OAuth token already exists (no network calls)."""
    return {
        "google": google_token_present(),
        "microsoft": microsoft_token_present(),
    }
