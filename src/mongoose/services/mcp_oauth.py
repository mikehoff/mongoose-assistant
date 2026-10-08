"""Minimal OAuth 2.1 authorization server for the Mongoose MCP endpoint, just enough for the Claude.ai
custom-connector flow (which offers only Name + URL + OAuth Client ID + Secret, no static-header option).

Single owner, single client (Claude.ai). Flow: Claude reads the discovery metadata, sends the owner's
browser to /authorize (auto-approved, the owner IS the only user and physically owns this server),
gets an auth code, then exchanges it at /token with the client secret + PKCE verifier for an access
token, which it then sends as Bearer on /mcp. Codes and tokens live in app_state so tokens survive a
restart (the owner does not have to re-approve after every Mongoose deploy).

Security rests on: the token exchange requires the client_secret AND the PKCE code_verifier (S256), and
the redirect_uri must be a claude.ai callback. Auto-approving /authorize is therefore safe, a stray
/authorize hit only ever delivers a code to claude.ai, and it is useless without the secret + verifier.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from typing import Any

from ..db import get_app_state, set_app_state

_CODE_TTL = 600            # auth code valid 10 min
_TOKEN_TTL = 30 * 86400    # access token valid 30 days
_CODES_KEY = "mcp_oauth_codes_v1"
_TOKENS_KEY = "mcp_oauth_tokens_v1"


def issuer() -> str:
    from .. import profile as _profile
    return (os.getenv("MONGOOSE_PUBLIC_URL") or _profile.public_url()).rstrip("/")


def _client() -> tuple[str, str]:
    return (os.getenv("MONGOOSE_MCP_OAUTH_CLIENT_ID", "").strip(),
            os.getenv("MONGOOSE_MCP_OAUTH_CLIENT_SECRET", "").strip())


# ── discovery metadata ───────────────────────────────────────────────────────
def authorization_server_metadata() -> dict[str, Any]:
    iss = issuer()
    return {
        "issuer": iss,
        "authorization_endpoint": f"{iss}/authorize",
        "token_endpoint": f"{iss}/token",
        "registration_endpoint": f"{iss}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
        "scopes_supported": ["mcp"],
    }


def protected_resource_metadata() -> dict[str, Any]:
    iss = issuer()
    return {"resource": f"{iss}/mcp", "authorization_servers": [iss]}


# ── code + token stores (app_state, JSON) ────────────────────────────────────
def _load(key: str) -> dict[str, Any]:
    try:
        return json.loads(get_app_state(key, "{}") or "{}")
    except Exception:
        return {}


def _prune(d: dict[str, Any], now: float) -> dict[str, Any]:
    return {k: v for k, v in d.items() if float(v.get("exp", 0)) > now}


def _b64url_sha256(s: str) -> str:
    dig = hashlib.sha256(s.encode()).digest()
    return base64.urlsafe_b64encode(dig).decode().rstrip("=")


# ── endpoints ─────────────────────────────────────────────────────────────────
def authorize(params: dict[str, str]) -> dict[str, Any]:
    """Validate an /authorize request and mint a one-time code. Returns {ok, redirect} to 302 to, or
    {ok:False, error, ...} to render. Auto-approves (single owner)."""
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    challenge = params.get("code_challenge", "")
    method = params.get("code_challenge_method", "")
    cid, _ = _client()
    if cid and client_id != cid:
        return {"ok": False, "error": "invalid_client", "detail": "client_id does not match"}
    if not redirect_uri.startswith("https://claude.ai/") and not redirect_uri.startswith("https://claude.com/"):
        return {"ok": False, "error": "invalid_redirect_uri", "detail": "redirect_uri must be a claude.ai callback"}
    if not challenge or method != "S256":
        return {"ok": False, "error": "invalid_request", "detail": "PKCE S256 code_challenge required"}
    now = time.time()
    code = secrets.token_urlsafe(32)
    codes = _prune(_load(_CODES_KEY), now)
    codes[code] = {"cc": challenge, "ru": redirect_uri, "cid": client_id, "exp": now + _CODE_TTL}
    set_app_state(_CODES_KEY, json.dumps(codes))
    sep = "&" if "?" in redirect_uri else "?"
    from urllib.parse import quote
    redirect = f"{redirect_uri}{sep}code={quote(code)}"
    if state:
        redirect += f"&state={quote(state)}"
    return {"ok": True, "redirect": redirect}


def exchange(form: dict[str, str], *, basic_auth: tuple[str, str] | None = None) -> dict[str, Any]:
    """Exchange an authorization code for an access token. Verifies client_secret + PKCE. Returns the
    token JSON, or {error:...} on failure."""
    grant = form.get("grant_type", "")
    if grant != "authorization_code":
        return {"error": "unsupported_grant_type"}
    code = form.get("code", "")
    verifier = form.get("code_verifier", "")
    redirect_uri = form.get("redirect_uri", "")
    client_id = form.get("client_id", "") or (basic_auth[0] if basic_auth else "")
    client_secret = form.get("client_secret", "") or (basic_auth[1] if basic_auth else "")
    cid, csec = _client()
    if cid and client_id != cid:
        return {"error": "invalid_client"}
    if csec and not secrets.compare_digest(client_secret, csec):
        return {"error": "invalid_client"}
    now = time.time()
    codes = _prune(_load(_CODES_KEY), now)
    c = codes.get(code)
    if not c:
        return {"error": "invalid_grant"}
    if c.get("ru") != redirect_uri:
        return {"error": "invalid_grant", "error_description": "redirect_uri mismatch"}
    if not verifier or _b64url_sha256(verifier) != c.get("cc"):
        return {"error": "invalid_grant", "error_description": "PKCE verification failed"}
    codes.pop(code, None)
    set_app_state(_CODES_KEY, json.dumps(codes))
    tok = secrets.token_urlsafe(32)
    toks = _prune(_load(_TOKENS_KEY), now)
    toks[tok] = {"exp": now + _TOKEN_TTL}
    set_app_state(_TOKENS_KEY, json.dumps(toks))
    return {"access_token": tok, "token_type": "Bearer", "expires_in": _TOKEN_TTL, "scope": "mcp"}


def register(body: dict[str, Any]) -> dict[str, Any]:
    """Dynamic client registration (RFC 7591), some MCP clients use it instead of pre-set creds. We
    return our single configured client so any registration resolves to the same credentials."""
    cid, csec = _client()
    return {
        "client_id": cid or "mongoose-mcp",
        "client_secret": csec,
        "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "redirect_uris": body.get("redirect_uris") or ["https://claude.ai/api/mcp/auth_callback"],
    }


def valid_access_token(token: str) -> bool:
    if not token:
        return False
    now = time.time()
    toks = _load(_TOKENS_KEY)
    rec = toks.get(token)
    return bool(rec and float(rec.get("exp", 0)) > now)
