from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import httpx
from .db import redact_reason

# Interactive setup for every Mongoose integration. Writes values to .env and
# verifies each one live where possible. Local-first and safe: it only writes
# the .env file you point it at and never sends your data anywhere except the
# service you are configuring (to verify the credential works).

def _stable_config_dir() -> Path:
    """The install-location-independent config directory. Config must NOT live relative to whatever
    directory the installer happened to run from. Order: explicit MONGOOSE_CONFIG_DIR, else ~/.mongoose.
    """
    override = os.environ.get("MONGOOSE_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".mongoose"


_MONGOOSE_ENV_KEY_PREFIXES = (
    "MONGOOSE_", "HA_", "HOME_ASSISTANT_", "IMAP_", "SMTP_", "DUCKDNS_", "DDNS_",
    "MICROSOFT_", "MS_", "GOOGLE_", "GMAIL_", "NTFY_", "PUSH_", "VAPID_",
    "WORDPRESS_", "WP_", "RTM_", "CLAUDE_", "OLLAMA_", "ALEXA_", "OPENAI_",
)

_MONGOOSE_ENV_KEYS = {
    "HA_URL", "HA_TOKEN", "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "IMAP_PORT",
    "DUCKDNS_DOMAIN", "DUCKDNS_TOKEN", "MICROSOFT_CLIENT_ID", "MICROSOFT_TENANT_ID",
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_PORT",
    "NTFY_TOPIC", "NTFY_SERVER", "WORDPRESS_BASE_URL", "WORDPRESS_URL",
    "RTM_WEBHOOK_SECRET", "RTM_WEBHOOK_KEY", "MONGOOSE_API_TOKEN",
}


def _mongoose_env_score(path: Path) -> int:
    """Return a confidence score that *path* is a real Mongoose config file.

    The migration must not slurp an arbitrary neighbouring project .env. Score only files carrying
    recognisable Mongoose/Home-Assistant/mail/DDNS/RTM integration keys, and ignore tiny/noisy files.
    """
    try:
        if not path.is_file():
            return 0
        text = path.read_text(errors="ignore")[:131072]
    except Exception:
        return 0
    score = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key in _MONGOOSE_ENV_KEYS:
            score += 3
        elif key.startswith(_MONGOOSE_ENV_KEY_PREFIXES):
            score += 2
        elif key in {"PORT", "HOST", "APP_URL"} and "MONGOOSE" in text.upper():
            score += 1
    return score


def _legacy_env_candidates(stable: Path) -> list[Path]:
    """Bounded search for old install-local .env files.

    Older builds wrote config to the installer working directory. A reinstall from a new extracted
    folder therefore leaves the real config in a sibling/parent install directory. We search only the
    current directory, a few parent/sibling install folders, and common user download/desktop roots.
    That is enough to preserve the old promise without doing an unbounded home-directory crawl.
    """
    seen: set[Path] = set()
    out: list[Path] = []

    def add(candidate: Path) -> None:
        try:
            resolved = candidate.expanduser().resolve()
            stable_resolved = stable.expanduser().resolve()
        except Exception:
            return
        if resolved == stable_resolved or resolved in seen:
            return
        seen.add(resolved)
        out.append(resolved)

    cwd = Path.cwd()
    add(cwd / ".env")
    for parent in [cwd, *list(cwd.parents)[:4]]:
        add(parent / ".env")
        try:
            for child in parent.glob("mongoose*/.env"):
                add(child)
            for child in parent.glob("Mongoose*/.env"):
                add(child)
            for child in parent.glob("mongoose*/mongoose/.env"):
                add(child)
            for child in parent.glob("Mongoose*/mongoose/.env"):
                add(child)
        except Exception:
            pass

    home = Path.home()
    for root in (home / "Downloads", home / "Desktop", home):
        if not root.exists():
            continue
        try:
            for child in root.glob("mongoose*/.env"):
                add(child)
            for child in root.glob("Mongoose*/.env"):
                add(child)
            for child in root.glob("mongoose*/mongoose/.env"):
                add(child)
            for child in root.glob("Mongoose*/mongoose/.env"):
                add(child)
        except Exception:
            pass
    return out


def _best_legacy_env(stable: Path) -> Path | None:
    scored: list[tuple[int, float, Path]] = []
    for candidate in _legacy_env_candidates(stable):
        score = _mongoose_env_score(candidate)
        if score <= 0:
            continue
        try:
            mtime = candidate.stat().st_mtime
        except Exception:
            mtime = 0.0
        scored.append((score, mtime, candidate))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


def _may_migrate_into(stable: Path) -> bool:
    """Only ever copy an old .env (it holds credentials) into the REAL user's config folder. A test, a
    sandbox or any process whose HOME was pointed at a temporary folder must never receive a copy, or
    every such run would leave the owner's secrets lying in /tmp."""
    try:
        import pwd
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        if not stable.expanduser().resolve().is_relative_to(real_home):
            return False
    except Exception:
        return False
    try:
        from .services.egress_guard import _under_test, _data_dir_is_ephemeral
        if _under_test() or _data_dir_is_ephemeral():
            return False
    except Exception:
        pass
    return True


def _resolve_env_path() -> Path:
    """The .env location, stable across installs.

    Explicit MONGOOSE_ENV_FILE wins. Otherwise use the stable config dir and, if it is empty, migrate the
    best-looking legacy .env from the current or a nearby previous Mongoose install folder.
    """
    explicit = os.environ.get("MONGOOSE_ENV_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    stable = _stable_config_dir() / ".env"
    try:
        if not stable.exists() and _may_migrate_into(stable):
            legacy = _best_legacy_env(stable)
            if legacy is not None:
                stable.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy2(legacy, stable)
    except Exception:
        pass
    return stable


ENV_PATH = _resolve_env_path()


def _read_env(path: Path | None = None) -> dict[str, str]:
    if path is None:
        path = ENV_PATH
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    return values


def effective_env(path: Path | None = None) -> dict[str, str]:
    """The env the wizard should judge configured-ness and verifiers against.

    Both the .env file and the process environment can supply a key. The rule that
    avoids the "saved but reads as not set" surprise: a NON-EMPTY value always wins,
    and a value the user just saved to .env is never clobbered by an EMPTY or absent
    process-env value. (os.environ can carry an empty-but-present key, e.g. exported
    blank by a launch wrapper or left from a prior partial run, which previously
    overrode the real .env value because os.environ was merged last.)

    Precedence per key: non-empty os.environ > non-empty .env > whatever's left.
    """
    import os
    if path is None:
        path = ENV_PATH
    file_env = _read_env(path)
    merged = dict(file_env)
    for k, v in os.environ.items():
        if v:  # only a non-empty process value may override; empty never clobbers .env
            merged[k] = v
        elif k not in merged:  # an empty process value only fills a gap, never overrides
            merged[k] = v
    return merged


def dedupe_env(path: Path = ENV_PATH) -> list[str]:
    """Collapse duplicate keys in .env, keeping each key's LAST value (which is
    the one dotenv actually uses) at the position of its FIRST occurrence.
    Comments and blank lines are preserved. Returns the list of keys that had
    duplicates removed. Safe to call repeatedly (idempotent).
    """
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    # Final value for each key = last assignment seen.
    last_value: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            last_value[key] = stripped.split("=", 1)[1].strip()
    written: set[str] = set()
    deduped: list[str] = []
    duplicates: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in written:
                duplicates.append(key)
                continue  # drop later duplicate occurrences
            deduped.append(f"{key}={last_value[key]}")
            written.add(key)
        else:
            deduped.append(line)
    if duplicates:
        path.write_text("\n".join(deduped) + "\n")
    # Return unique keys that were de-duplicated, preserving order.
    seen: set[str] = set()
    return [k for k in duplicates if not (k in seen or seen.add(k))]


def write_env(updates: dict[str, str], path: Path | None = None) -> None:
    """Merge updates into .env, preserving existing lines and comments.

    Values containing newlines or carriage returns are rejected to prevent
    .env line injection.
    """
    if path is None:
        path = ENV_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    for k, v in updates.items():
        if "\n" in v or "\r" in v:
            raise ValueError(f"Refusing to write multiline value for {k}")
    # Self-heal any pre-existing duplicate keys before merging.
    dedupe_env(path)
    lines = path.read_text().splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    path.write_text("\n".join(out) + "\n")


# --- Verifiers: return (ok, message). Pure functions of env values. ----------

def verify_home_assistant(env: dict[str, str]) -> tuple[bool, str]:
    url, token = env.get("HA_URL"), env.get("HA_TOKEN")
    if not url or not token:
        return False, "HA_URL or HA_TOKEN not set"
    # Normalise common input formats so the user doesn't have to know the exact scheme/port.
    # Bare IP like 192.168.1.20 -> http://192.168.1.20:8123
    import re as _re
    url = url.strip()
    if _re.match(r"^\d{1,3}(\.\d{1,3}){3}$", url):          # bare IPv4 e.g. 192.168.1.20
        url = f"http://{url}:8123"
    elif _re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d+$", url):    # IP:port e.g. 192.168.1.20:8123
        url = f"http://{url}"
    elif not url.startswith("http"):
        url = f"http://{url}"
    # Warn immediately about mDNS names before even trying (the server-side process can't resolve them).
    host = url.split("//")[-1].split("/")[0].split(":")[0]
    if host.endswith(".local"):
        return False, (
            f"'{host}' is an mDNS name (like homeassistant.local) that this app can't resolve, "
            f"your browser can, but the Python server process can't. "
            f"Use the IP address instead: find it in HA under Settings → System → Network, "
            f"e.g. http://192.168.1.20:8123"
        )
    try:
        r = httpx.get(f"{url.rstrip('/')}/api/", headers={"Authorization": f"Bearer {token}"}, timeout=5)
        if r.status_code == 200:
            return True, "Home Assistant responded 200 (connected)"
        if r.status_code in (401, 403):
            return False, "Home Assistant reachable but rejected the token (401/403). Re-copy a fresh Long-Lived Access Token."
        return False, f"Home Assistant responded {r.status_code} (reachable but not the expected 200)"
    except Exception as exc:
        msg = str(exc).lower()
        host = url.split("//")[-1].split("/")[0].split(":")[0]
        # The most common real-world failure: a .local (mDNS) hostname that a Python process can't
        # resolve even though the browser/phone can. Tell the user exactly what to do.
        if ("nodename" in msg or "name or service not known" in msg or "getaddrinfo" in msg or
                "no address" in msg or "temporary failure in name resolution" in msg):
            extra = ""
            if host.endswith(".local"):
                extra = (f" The name '{host}' is an mDNS/Bonjour name that this app often can't "
                         f"resolve (your browser can, but the server can't). Use Home Assistant's "
                         f"IP address instead, e.g. http://192.168.1.50:8123, find it in HA under "
                         f"Settings, System, Network.")
            return False, f"Couldn't resolve the Home Assistant address ('{host}').{extra}"
        if "connection refused" in msg:
            return False, (f"Connection refused at {url}. Check the port (HA is usually :8123) and "
                           f"that Home Assistant is running and reachable from this Mac.")
        if "timed out" in msg or "timeout" in msg:
            return False, f"Timed out reaching {url}. Check the address/port and that the Mac can reach HA on your network."
        return False, f"Could not reach Home Assistant: {exc}"


def verify_anthropic(env: dict[str, str]) -> tuple[bool, str]:
    key = env.get("ANTHROPIC_API_KEY")
    if not key:
        return False, "ANTHROPIC_API_KEY not set"
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": env.get("ANTHROPIC_INSTANT_MODEL", "claude-haiku-4-5-20251001"),
                  "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
            timeout=10,
        )
        return (r.status_code in (200, 400), f"Anthropic responded {r.status_code}")
    except Exception as exc:
        return False, f"Could not reach Anthropic: {exc}"


def verify_openai(env: dict[str, str]) -> tuple[bool, str]:
    key = env.get("OPENAI_API_KEY")
    if not key:
        return False, "OPENAI_API_KEY not set"
    try:
        r = httpx.get("https://api.openai.com/v1/models",
                      headers={"Authorization": f"Bearer {key}"}, timeout=10)
        return (r.status_code == 200, f"OpenAI responded {r.status_code}")
    except Exception as exc:
        return False, redact_reason(f"Could not reach OpenAI: {exc}")


def verify_google_api(env: dict[str, str]) -> tuple[bool, str]:
    """Verify BOTH Google OAuth readiness (for Calendar/Gmail/Tasks) AND the Gemini
    API key (for cloud escalation), and report them separately so a user isn't told
    'Google is fine' when only the Gemini key is set but OAuth consent is missing."""
    import os
    parts: list[str] = []
    overall = False

    # OAuth side: is there a client-secrets file AND a captured token?
    secrets_path = env.get("GOOGLE_CLIENT_SECRETS_PATH", "./secrets/google-client-secrets.json")
    token_candidates = ["./secrets/google-token.json", "./secrets/gmail-token.json",
                        os.getenv("GOOGLE_TOKEN_PATH", "")]
    have_secrets = bool(secrets_path) and os.path.exists(secrets_path)
    have_token = any(p and os.path.exists(p) for p in token_candidates)
    if not have_secrets:
        parts.append("OAuth: client secrets JSON not found (Calendar/Gmail/Tasks won't work)")
    elif not have_token:
        parts.append("OAuth: secrets present but NOT yet authorised. Open the Setup tab and click Authorise for Google.")
    else:
        parts.append("OAuth: authorised")
        overall = True

    # Gemini side: optional, only if a key is set.
    key = env.get("GOOGLE_API_KEY")
    if not key:
        parts.append("Gemini: no API key (optional, cloud escalation off)")
    else:
        try:
            r = httpx.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}", timeout=10)
            parts.append("Gemini: key OK" if r.status_code == 200 else f"Gemini: key error {r.status_code}")
        except Exception as exc:
            parts.append(redact_reason(f"Gemini: could not reach Google: {exc}"))

    return overall, "; ".join(parts)


def verify_imap(env: dict[str, str]) -> tuple[bool, str]:
    host, user, pw = env.get("IMAP_HOST"), env.get("IMAP_USERNAME"), env.get("IMAP_PASSWORD")
    if not (host and user and pw):
        return False, "IMAP_HOST/USERNAME/PASSWORD not all set"
    try:
        import imaplib
        m = imaplib.IMAP4_SSL(host)
        m.login(user, pw)
        m.logout()
        return True, "IMAP login succeeded"
    except Exception as exc:
        return False, f"IMAP login failed: {exc}"


def verify_alexa(env: dict[str, str]) -> tuple[bool, str]:
    url, token = env.get("ALEXA_FALLBACK_URL"), env.get("ALEXA_FALLBACK_TOKEN")
    if not (url and token):
        return False, "Alexa bridge not configured (optional)"
    return True, "Alexa bridge values present (not live-tested)"


def _setup_saved(env: dict[str, str], slug: str) -> bool:
    key = "MONGOOSE_SETUP_SAVED_" + slug.upper().replace("-", "_")
    return (env.get(key) or "").strip().lower() in {"1", "true", "yes", "on"}


def _ntfy_configured(env: dict[str, str]) -> bool:
    """A saved default is a saved choice, but copied defaults alone are still only scaffolding."""
    url = (env.get("NTFY_URL") or "").strip()
    topic = (env.get("NTFY_TOPIC") or "").strip()
    token = (env.get("NTFY_TOKEN") or "").strip()
    if not (url and topic):
        return False
    if _setup_saved(env, "push"):
        return True
    return bool(token or url != "http://127.0.0.1:8090" or topic != "mongoose-private-topic")


def _scheduler_configured(env: dict[str, str]) -> bool:
    """Scheduler defaults count once saved, OR once the scheduler is verifiably running. Field
    bug this replaces: leaving the sensible defaults untouched (the correct thing to do for most
    people) meant this could never show as configured, forever, even while the scheduler ran
    perfectly fine -- "configured" was inferred purely from whether optional values had been
    edited, never from whether the thing actually works."""
    if _setup_saved(env, "scheduler"):
        return True
    defaults = {
        "MONGOOSE_BRIEF_TIME": "07:30",
        "MONGOOSE_EMAIL_SYNC_MINUTES": "30",
        "MONGOOSE_REINDEX_MINUTES": "360",
        "MONGOOSE_INDEX_ROOTS": "",
    }
    if any((env.get(k) or "").strip() and (env.get(k) or "").strip() != default
           for k, default in defaults.items()):
        return True
    try:
        from .services.scheduler import _scheduler_running
        return bool(_scheduler_running())
    except Exception:
        return False


def verify_ntfy(env: dict[str, str]) -> tuple[bool, str]:
    url, topic = env.get("NTFY_URL"), env.get("NTFY_TOPIC")
    if not (url and topic):
        return False, "NTFY_URL/NTFY_TOPIC not set (push to phone disabled)"
    # Local-first: if the ntfy server isn't local, notification content (titles/bodies, e.g. the
    # daily brief) transits a third party. Surface that AT CONFIGURATION TIME, not just in help text
    # the user read once, a successful test must not silently imply the privacy posture is intact.
    is_local = any(h in url for h in ("127.0.0.1", "localhost", "192.168.", "10.", "::1"))
    try:
        import urllib.request
        headers = {"Title": "Mongoose setup", "Tags": "white_check_mark"}
        token = env.get("NTFY_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"{url.rstrip('/')}/{topic}",
                                     data=b"Push configured. You should see this on your phone.",
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            ok = resp.status < 400
        if not ok:
            return False, "ntfy returned an error"
        if is_local:
            return True, "Sent a test push, check your phone (local ntfy, content stays on your network)"
        return True, ("Sent a test push, check your phone. NOTE: this ntfy server is not local, so "
                      "notification content (e.g. the daily brief) transits a third party. Use a "
                      "local ntfy server to keep it on your network.")
    except Exception as exc:
        msg = str(exc).lower()
        if "refused" in msg or "connection refused" in msg or "[errno 61]" in msg or "[errno 111]" in msg:
            if is_local:
                return False, ("No local ntfy server is running yet, so there's nothing to push to. "
                               "In the Cockpit, open Notifications and push, then click 'Set up native "
                               "push': Mongoose installs a private local ntfy for you (no Docker, content "
                               "stays on your network). Then test again.")
            return False, (f"Couldn't reach the ntfy server at {url} (connection refused). Check the "
                           "URL, or use the Cockpit's Notifications and push panel to set up a local "
                           "server instead.")
        if "timed out" in msg or "timeout" in msg:
            return False, f"Timed out reaching the ntfy server at {url}. Check the URL and that the server is running."
        return False, f"ntfy unreachable: {exc}"


def _searxng_configured(env: dict[str, str]) -> bool:
    # Ships enabled by default (pointed at the local container); only "off" when explicitly emptied.
    return bool(env.get("MONGOOSE_SEARXNG_URL", "http://127.0.0.1:8888").strip())


def verify_searxng(env: dict[str, str]) -> tuple[bool, str]:
    """SearXNG powers web search and the knowledge-import insight research. It ships pointed at a LOCAL
    container so queries stay on your network. Ping it so setup tells you whether it actually works."""
    url = env.get("MONGOOSE_SEARXNG_URL", "http://127.0.0.1:8888").strip()
    if not url:
        return False, ("Web search is OFF (MONGOOSE_SEARXNG_URL is empty). Insight research stays "
                       "local-only. Set it to a private SearXNG instance to enable web evidence.")
    is_local = any(h in url for h in ("127.0.0.1", "localhost", "192.168.", "10.", "::1"))
    try:
        import urllib.request
        req = urllib.request.Request(f"{url.rstrip('/')}/search?q=ping&format=json",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 403:
                return False, (f"SearXNG at {url} rejected the json format (403). Enable it in "
                               "settings.yml: search: formats: [html, json].")
            if resp.status >= 400:
                return False, f"SearXNG at {url} returned HTTP {resp.status}."
        where = "local, queries stay on your network" if is_local else \
                "NOT local, so those queries leave your network"
        return True, f"Web search is working via SearXNG at {url} ({where})."
    except Exception as exc:
        msg = str(exc).lower()
        if "refused" in msg or "[errno 61]" in msg or "[errno 111]" in msg:
            return False, (f"SearXNG isn't running at {url} yet. It's defined in your install's compose "
                           "file, so  mongoose start-docker  launches it; then test again. (Web search "
                           "and insight research stay local-only until it's up.)")
        if "timed out" in msg or "timeout" in msg:
            return False, f"Timed out reaching SearXNG at {url}. Check the URL and that the container is running."
        return False, f"SearXNG unreachable at {url}: {exc}"

class Field:
    def __init__(self, key: str, prompt: str, secret: bool = False, optional: bool = False,
                 default: str = "", hint: str = ""):
        self.key = key
        self.prompt = prompt
        self.secret = secret
        self.optional = optional
        self.default = default
        # Short plain-language sub-text shown under the input: what it's for, why it's optional, what
        # a prefilled default means. Keeps the field label terse while still explaining itself.
        self.hint = hint


class Integration:
    def __init__(self, slug: str, name: str, help_text: str, fields: list[Field],
                 verifier: Callable[[dict[str, str]], tuple[bool, str]] | None = None,
                 configured_when: Callable[[dict[str, str]], bool] | None = None,
                 auth_state: Callable[[dict[str, str]], str] | None = None):
        self.slug = slug
        self.name = name
        self.help_text = help_text
        self.fields = fields
        self.verifier = verifier
        # Optional predicate for genuine configuration. Use it when a field has a default value
        # (so "the default string is present" must NOT count as configured), or when configuration
        # means something concrete like "the OAuth file actually exists". When absent, the default
        # rule applies: at least one field set to a NON-DEFAULT, non-empty value, and (if any field
        # is required) all required fields present.
        self.configured_when = configured_when
        # Optional THREE-STATE auth model for OAuth integrations: returns "signed_in",
        # "needs_signin", or "not_set". This is what stops the UI showing "configured" when only
        # the client JSON / client ID exists but no token has been captured yet. When present, it is
        # the source of truth for the tile state; "configured" then means "signed_in".
        self.auth_state = auth_state


def _oauth_token_present(candidates: list[str]) -> bool:
    """True if any of the given token files exists (a real captured OAuth token)."""
    import os as _os
    return any(p and _os.path.exists(_os.path.expanduser(p)) for p in candidates if p)


def _google_token_paths() -> list[str]:
    """The real Google token path (from settings) plus legacy fallbacks, so state detection
    matches wherever oauth.authorize_google actually wrote the token."""
    paths = ["./secrets/google-token.json", "./secrets/gmail-token.json",
             "./secrets/google-oauth-token.json", "./secrets/token.json"]
    try:
        from .settings import get_settings
        p = get_settings().google_token_path
        if p:
            paths.insert(0, p)
    except Exception:
        pass
    return paths


def _ms_token_paths() -> list[str]:
    paths = ["./secrets/msal-token-cache.bin", "./secrets/ms-token-cache.json",
             "./secrets/msal-token-cache.json", "./secrets/microsoft-token.json"]
    try:
        from .settings import get_settings
        p = get_settings().ms_token_cache
        if p:
            paths.insert(0, p)
    except Exception:
        pass
    return paths


def _google_calendar_auth_state(env: dict[str, str]) -> str:
    try:
        from .oauth import google_token_present
        signed_in = google_token_present()
    except Exception:
        signed_in = _oauth_token_present(_google_token_paths())
    if signed_in:
        return "signed_in"
    if _file_exists(env.get("GOOGLE_CLIENT_SECRETS_PATH", "")):
        return "needs_signin"
    return "not_set"


def _microsoft_auth_state(env: dict[str, str]) -> str:
    try:
        from .oauth import microsoft_token_present
        signed_in = microsoft_token_present()
    except Exception:
        signed_in = _oauth_token_present(_ms_token_paths())
    if signed_in:
        return "signed_in"
    # To Do works WITHOUT a registered app (the connector falls back to Microsoft's well-known public
    # client), so a sign-in is always available, show needs_signin rather than not_set, whether or not
    # MS_CLIENT_ID is set.
    return "needs_signin"


def _file_exists(path_value: str) -> bool:
    """True if a configured path actually points at an existing file. Empty -> False."""
    if not path_value:
        return False
    try:
        return Path(path_value).expanduser().exists()
    except Exception:
        return False


INTEGRATIONS: list[Integration] = [
    Integration(
        "home_assistant", "Home Assistant",
        "Connects Mongoose to your Home Assistant so it can read and control your home. In HA: "
        "click your profile (bottom-left), Security tab, Long-Lived Access Tokens, Create Token. "
        "Use HA's IP address (not homeassistant.local, which this app often can't resolve).",
        [Field("HA_URL", "Home Assistant URL",
               hint="Use the IP form, e.g. http://192.168.1.50:8123. Find the IP in HA under "
                    "Settings, System, Network. Avoid http://homeassistant.local:8123, the browser "
                    "can resolve that but this app usually can't."),
         Field("HA_TOKEN", "Long-lived access token", secret=True,
               hint="The token you created above. It's long; paste the whole thing.")],
        verify_home_assistant,
    ),
    Integration(
        "alexa", "Alexa bridge",
        "Most people skip this. It's only for advanced setups where you run your own Alexa "
        "proxy/webhook to let Mongoose trigger Alexa routines. Mongoose never sends private content "
        "to Alexa. If you don't run such a proxy, leave both fields blank, nothing breaks.",
        [Field("ALEXA_FALLBACK_URL", "Alexa bridge URL", optional=True,
               hint="The URL of your own Alexa proxy/webhook. Leave blank unless you run one."),
         Field("ALEXA_FALLBACK_TOKEN", "Alexa bridge token", secret=True, optional=True,
               hint="Auth token for that proxy, if it needs one.")],
        verify_alexa,
    ),
    Integration(
        "google_calendar", "Google Calendar / Tasks",
        "Optional. Lets Mongoose see your Google Calendar and Tasks so they appear in your brief and "
        "search. This needs an OAuth client file (a one-time setup). Steps: at "
        "https://console.cloud.google.com, APIs & Services, Credentials, Create credentials, OAuth "
        "client ID, Application type Desktop app, Create, then Download JSON and save it to the path "
        "below. Leave blank to skip calendar/tasks. (This is separate from Gemini below.)",
        [Field("GOOGLE_CLIENT_SECRETS_PATH", "Path to OAuth client JSON",
               default="./secrets/google-client-secrets.json", optional=True,
               hint="The downloaded OAuth client JSON file (step above). Saving it is only the first "
                    "step, you then click 'Sign in with Google' to authorise. It does not count as "
                    "signed in until you complete sign-in.")],
        verifier=None,
        # 'configured' (i.e. usable) ONLY when a real OAuth TOKEN exists, not merely the client file.
        # The middle state (file saved, not yet signed in) is surfaced via auth_state below.
        configured_when=lambda env: _google_calendar_auth_state(env) == "signed_in",
        auth_state=_google_calendar_auth_state,
    ),
    Integration(
        "gemini", "Gemini (cloud model, optional)",
        "Optional, and separate from Google Calendar above. A Gemini API key lets Mongoose escalate "
        "to Google's cloud model only when a local answer is uncertain and you opt in, within the "
        "cost cap. No key = everything stays on local models. Get a key at "
        "https://aistudio.google.com/apikey.",
        [Field("GOOGLE_API_KEY", "Gemini API key", secret=True, optional=True,
               hint="From aistudio.google.com/apikey. Optional, leave blank to keep everything "
                    "on local models. This does NOT affect calendar/tasks.")],
        verifier=verify_google_api,
        configured_when=lambda env: bool(env.get("GOOGLE_API_KEY")),
    ),
    Integration(
        "microsoft", "Microsoft Calendar / To Do",
        "Sync your Microsoft To Do tasks (and optionally Outlook calendar) into Mongoose. "
        "For TO DO, you do NOT need to register an app or get a developer sandbox, just click "
        "'Sign in with Microsoft' below: Mongoose shows you a short code, you enter it at "
        "microsoft.com/devicelogin, approve Tasks.Read, and your tasks sync. That's it. "
        "(Optional, advanced: if you want Outlook CALENDAR or MAIL too, or prefer the sign-in to show "
        "your own app name, register an app at entra.microsoft.com, New registration, account types "
        "including personal Microsoft accounts, add Calendars.Read/Tasks.Read, and put its Application "
        "(client) ID below. This is optional; To Do works without it.) "
        "Note: IMAP (the Email tile) can sync email but never tasks or calendar, those need this "
        "Microsoft sign-in.",
        [Field("MS_CLIENT_ID", "Application (client) ID", optional=True,
               hint="From your app's Overview page at entra.microsoft.com after you register it. "
                    "Saving it is scaffolding, you still click 'Sign in with Microsoft'. NOTE: if "
                    "the directory wall blocks you, IMAP can replace your EMAIL but NOT your To Do "
                    "tasks or calendar, those need this Microsoft integration (or Google)."),
         Field("MS_TENANT_ID", "Directory (tenant) ID", default="common", optional=True,
               hint="An authority value, not proof of configuration. 'common' works for personal and "
                    "most work accounts; 'consumers' is personal-only; 'organizations' is work/school; "
                    "or your specific tenant ID. Leave as 'common' unless your IT department says "
                    "otherwise.")],
        configured_when=lambda env: _microsoft_auth_state(env) == "signed_in",
        auth_state=_microsoft_auth_state,
    ),
    Integration(
        "imap", "Email (IMAP, read-only)",
        "Lets Mongoose read your email (read-only) so it can include it in the daily brief. Use an "
        "app-specific password, never your main password. For Gmail, create one at "
        "myaccount.google.com/apppasswords. Optional, skip if you don't want email.",
        [Field("IMAP_HOST", "IMAP host", optional=True,
               hint="e.g. imap.gmail.com for Gmail, outlook.office365.com for Outlook."),
         Field("IMAP_USERNAME", "Email address", optional=True,
               hint="Your full email address."),
         Field("IMAP_PASSWORD", "App password", secret=True, optional=True,
               hint="An app-specific password, not your normal login password.")],
        verify_imap,
    ),
    Integration(
        "anthropic", "Claude (cloud escalation)",
        "Optional. Mongoose runs on local models by default. If you add a Claude key, Mongoose can "
        "escalate to Claude ONLY when a local answer looks uncertain AND you've opted in for that "
        "turn, and only within a per-turn cost cap. No key = everything stays local. Get a key at "
        "console.anthropic.com, under API Keys.",
        [Field("ANTHROPIC_API_KEY", "Anthropic API key", secret=True, optional=True,
               hint="From console.anthropic.com, API Keys, Create Key. Optional, leave blank to "
                    "keep everything on local models.")],
        verify_anthropic,
    ),
    Integration(
        "openai", "OpenAI (cloud escalation fallback)",
        "Optional, same idea as the Claude key but using OpenAI as the cloud fallback. Only used on "
        "opt-in escalation within the cost cap. Leave blank to stay fully local. Get a key at "
        "platform.openai.com, under API keys.",
        [Field("OPENAI_API_KEY", "OpenAI API key", secret=True, optional=True,
               hint="From platform.openai.com/api-keys. Optional, leave blank to stay local.")],
        verify_openai,
    ),
    Integration(
        "push", "Push to your phone (ntfy)",
        "Sends notifications (like the daily brief) to your phone. Click 'Set up native push' below and "
        "Mongoose installs and configures a private LOCAL ntfy for you, no terminal needed. The URL "
        "below defaults to that local server so notification content never leaves your network.",
        [Field("NTFY_URL", "ntfy server URL", default="http://127.0.0.1:8090", optional=True,
               hint="Prefilled with a LOCAL server (127.0.0.1:8090) so push content stays on your "
                    "network. Keep this unless you specifically want a public server like "
                    "https://ntfy.sh (which would send notification content through a third party)."),
         Field("NTFY_TOPIC", "Topic name", default="mongoose-private-topic", optional=True,
               hint="A private name you choose, e.g. mongoose-7a2f. You subscribe the ntfy phone "
                    "app to this same name so pushes reach your phone. 'Set up native push' "
                    "generates one for you."),
         Field("NTFY_TOKEN", "ntfy auth token", secret=True, optional=True,
               hint="Only needed if your ntfy server requires authentication. Usually blank.")],
        verify_ntfy,
        configured_when=_ntfy_configured,
    ),
    Integration(
        "searxng", "Web search (SearXNG)",
        "Lets Mongoose research current or sensitive topics, and powers the knowledge-import insight "
        "research, through a private SearXNG metasearch instance instead of leaking queries to a third "
        "party. It ships pointed at a LOCAL SearXNG container (127.0.0.1:8888) that your install's "
        "compose file already defines, so you usually just need Docker running to launch it "
        "('mongoose start-docker'). Set the URL empty to turn web search off entirely.",
        [Field("MONGOOSE_SEARXNG_URL", "SearXNG base URL", default="http://127.0.0.1:8888", optional=True,
               hint="A SearXNG instance you control. Prefilled with the LOCAL container so search "
                    "queries stay on your network. Leave empty to disable web search. A public "
                    "instance must be https and have the json format enabled.")],
        verify_searxng,
        configured_when=_searxng_configured,
    ),
    Integration(
        "scheduler", "Background tasks (scheduler)",
        "Lets Mongoose run recurring jobs on its own, the daily brief, periodic email sync, and "
        "file reindexing. These settings just set WHEN. The scheduler runs as a background service "
        "that the installer normally sets up for you automatically, so for normal use you only need "
        "to adjust the times here. The Scheduler tab shows whether it's running and, if not, how to "
        "install it.",
        [Field("MONGOOSE_BRIEF_TIME", "Daily brief time (HH:MM, 24h)", default="07:30", optional=True,
               hint="When the morning brief is prepared, your local time. Default 07:30."),
         Field("MONGOOSE_EMAIL_SYNC_MINUTES", "Email sync interval (minutes)", default="30", optional=True,
               hint="How often to check email. Default every 30 minutes."),
         Field("MONGOOSE_REINDEX_MINUTES", "File reindex interval (minutes)", default="360", optional=True,
               hint="How often to re-scan your indexed folders. Default every 6 hours."),
         Field("MONGOOSE_INDEX_ROOTS", "Folders to index", optional=True,
               hint="Folders Mongoose should make searchable, e.g. /Users/you/Documents. Comma-"
                    "separated for several. Leave blank if you don't want file search.")],
        None,
        configured_when=_scheduler_configured,
    ),
    Integration(
        "remote", "Remote access (let your phone/tablet reach Mongoose)",
        "REQUIRED for phone pairing and phone push to work. By default Mongoose only listens on this "
        "Mac (so a phone can't reach it, that's the cause of 'connection refused' when pairing). To "
        "let your phone connect over your home network, set this to allow LAN access and give it a "
        "token. Then re-open the pairing link on your phone.",
        [Field("MONGOOSE_HOST", "Listen address", default="127.0.0.1", optional=True,
               hint="127.0.0.1 = this Mac only (phones can't reach it). Set to 0.0.0.0 to also "
                    "listen on your home network so your phone/tablet can connect. Use 0.0.0.0 if "
                    "you want phone pairing or push to work."),
         Field("MONGOOSE_ALLOW_REMOTE", "Allow network access?", default="false", optional=True,
               hint="Type true to allow your phone/tablet/HA to connect over the LAN, or false for "
                    "this-Mac-only. Phone pairing needs this set to true."),
         Field("MONGOOSE_API_TOKEN", "Shared access token for devices", secret=True, optional=True,
               hint="A password each device sends on every request, so only your devices can "
                    "connect. Make up a long random string, or leave blank to auto-generate one.")],
        None,
    ),
    Integration(
        "ddns", "Reachable by a stable name (when your home IP changes)",
        "Optional, for reaching Mongoose from outside your home when your ISP changes your IP. Run "
        "`mongoose ddns setup` in a terminal to choose a provider: Tailscale (a private VPN, "
        "recommended: no port-forwarding, no public IP exposure), or a dynamic-DNS service (DuckDNS, "
        "Dynu, No-IP, or Cloudflare, which keep a hostname pointed at your home IP but need a router "
        "port-forward and expose your public IP). Once set up, Mongoose updates the record "
        "automatically on a schedule. The fields below are filled in by `ddns setup`; you don't edit "
        "them here.",
        [Field("DDNS_PROVIDER", "Chosen provider", optional=True,
               hint="Set by `mongoose ddns setup` (tailscale / duckdns / dynu / noip / cloudflare)."),
         Field("DDNS_ENABLED", "Auto-update enabled", default="false", optional=True,
               hint="true once a provider is set up; the scheduler then keeps the IP current.")],
        None,
        configured_when=lambda env: bool(env.get("DDNS_PROVIDER")),
    ),
]


def merge_new_env_keys(env_path: Path = ENV_PATH, example_path: Path = Path(".env.example")) -> list[str]:
    """Add keys present in .env.example but missing from .env, using the
    example's value/comment as the default. Never overwrites existing values.
    Returns the list of keys that were added.
    """
    if not example_path.exists():
        return []
    # Collapse any pre-existing duplicates first, so the merge reads a clean file
    # and we never stack another copy of a key that already appears twice.
    dedupe_env(env_path)
    existing = set(_read_env(env_path).keys())
    added: list[str] = []
    to_append: list[str] = []
    pending_comments: list[str] = []
    for line in example_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped:
            pending_comments = []
            continue
        if stripped.startswith("#"):
            pending_comments.append(line)
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key not in existing:
                to_append.extend(pending_comments)
                to_append.append(line)
                added.append(key)
            pending_comments = []
    if to_append:
        current = env_path.read_text() if env_path.exists() else ""
        if current and not current.endswith("\n"):
            current += "\n"
        block = "\n# --- added by mongoose update (new defaults; edit as needed) ---\n" + "\n".join(to_append) + "\n"
        env_path.write_text(current + block)
    return added


def integration_status(*, verify: bool = False) -> list[dict[str, Any]]:
    """Current configured/verified state of every integration, for the browser
    page and `mongoose setup --status`.

    Status is observational by default. Earlier builds ran live verifiers here, which meant a harmless
    status poll could call remote APIs, send a test push, hang on DNS/IMAP, or 500 the setup page.
    Pass verify=True only for an explicit "test now" path.
    """
    env = effective_env()
    out = []
    for integ in INTEGRATIONS:
        try:
            if integ.configured_when is not None:
                configured = bool(integ.configured_when(env))
            else:
                # Default rule, fixed: a field counts as "set" only if it has a NON-EMPTY value that
                # ISN'T just the field's default (so a defaulted path/tenant alone never reads as
                # configured), AND all REQUIRED (non-optional) fields are set. An integration whose
                # fields are all optional needs at least one genuinely-set field, never all([])=True.
                def _is_set(f):
                    v = env.get(f.key, "")
                    return bool(v) and v != f.default
                required = [f for f in integ.fields if not f.optional]
                required_ok = all(_is_set(f) for f in required) if required else False
                any_real = any(_is_set(f) for f in integ.fields)
                configured = (required_ok or (not required and any_real)) and any_real
        except Exception:
            configured = False
        result = None
        if verify and integ.verifier and any(env.get(f.key) for f in integ.fields):
            try:
                ok, msg = integ.verifier(env)
                result = {"ok": ok, "message": msg}
            except Exception as exc:
                result = {"ok": False, "message": f"verification failed: {exc}"}
        # Three-state auth model for OAuth integrations (overrides the binary configured flag in
        # the UI): "signed_in" | "needs_signin" | "not_set". None for non-OAuth integrations.
        try:
            auth = integ.auth_state(env) if integ.auth_state else None
        except Exception:
            auth = None
        out.append({
            "slug": integ.slug, "name": integ.name, "help": integ.help_text,
            "configured": bool(configured), "verification": result,
            "auth_state": auth,
            "fields": [{"key": f.key, "secret": f.secret, "optional": f.optional,
                        "hint": f.hint, "default": f.default} for f in integ.fields],
        })
    return out


def run_wizard() -> None:
    """Interactive, staged terminal wizard. Walks the integrations in logical
    stages, writes .env, verifies each, and finishes with a readiness report so you
    know what actually works."""
    import getpass

    # Stage grouping so setup reads as a guided flow, not a flat list. Derived from the
    # SAME WIZARD_STAGES the browser wizard uses, so the CLI and browser can never drift
    # (they did: the CLI used to call Home Assistant "Core", contradicting the vision that
    # core = local brain + database, not the home). HA is an optional Home domain. (QA-audit
    # P0.2)
    from .wizard import WIZARD_STAGES
    stages: list[tuple[str, str, list[str]]] = [
        (st["title"], st.get("blurb", ""), list(st["slugs"])) for st in WIZARD_STAGES
    ]
    by_slug = {i.slug: i for i in INTEGRATIONS}

    print("\n" + "=" * 60)
    print("  Mongoose setup")
    print("=" * 60)
    print("\nMongoose's CORE is already here: the local brain (the model) and its")
    print("database, running on this machine. Everything below is OPTIONAL, it")
    print("connects extra sources and surfaces. Home Assistant is one optional")
    print("domain, not the core.\n")
    print("For each one you can configure it, skip it (press Enter / answer N), or")
    print("come back later by re-running `mongoose setup`. Secrets are hidden as you type.\n")

    env = _read_env()
    configured: list[str] = []
    skipped: list[str] = []

    for stage_no, (stage_name, stage_desc, slugs) in enumerate(stages, 1):
        print(f"\n{'-' * 60}")
        print(f"STAGE {stage_no}/{len(stages)}: {stage_name}")
        print(f"  {stage_desc}")
        print("-" * 60)
        for slug in slugs:
            integ = by_slug.get(slug)
            if integ is None:
                continue
            print(f"\n[{integ.name}]")
            print(_indent(integ.help_text))
            choice = input(f"Configure {integ.name}? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                skipped.append(integ.name)
                continue
            updates: dict[str, str] = {}
            for f in integ.fields:
                existing = env.get(f.key, f.default)
                shown = "(set)" if (existing and f.secret) else (existing or "")
                label = f"  {f.prompt}" + (f" [{shown}]" if shown else "") + ": "
                val = getpass.getpass(label) if f.secret else input(label)
                val = val.strip()
                if val:
                    updates[f.key] = val
                elif existing and f.key not in env:
                    updates[f.key] = existing
            if updates:
                write_env(updates)
                env.update(updates)
                print("  Saved.")
                if integ.verifier:
                    ok, msg = integ.verifier({**env, **os.environ})
                    print(f"  {'[OK]  ' if ok else '[warn]'} {msg}")
                _maybe_oauth(integ, env)
                configured.append(integ.name)
            else:
                skipped.append(integ.name)

    print("\n" + "=" * 60)
    print("  Setup summary")
    print("=" * 60)
    print(f"  Configured: {', '.join(configured) if configured else '(none)'}")
    print(f"  Skipped:    {', '.join(skipped) if skipped else '(none)'}")
    print("\nRunning a readiness check...\n")
    _print_doctor_summary()
    print("\nDone. You can add more any time from the Cockpit's Setup tab.")
    print("Mongoose keeps itself running in the background, so there is nothing else to start.")
    print("On your phone, subscribe the ntfy app to your topic to receive pushes.\n")


def _indent(text: str, prefix: str = "  ") -> str:
    import textwrap
    return "\n".join(prefix + line for line in textwrap.wrap(text, width=72))


def _maybe_oauth(integ: "Integration", env: dict[str, str]) -> None:
    """Offer to complete Google/Microsoft consent during setup."""
    if integ.slug == "google_calendar" and env.get("GOOGLE_CLIENT_SECRETS_PATH"):
        if input("  Authorise Google now (opens a browser)? [y/N] ").strip().lower() in ("y", "yes"):
            from .oauth import authorize_google
            ok, msg = authorize_google()
            print(f"  {'[OK]  ' if ok else '[warn]'} {msg}")
    if integ.slug == "microsoft" and env.get("MS_CLIENT_ID"):
        if input("  Authorise Microsoft now (shows a device code)? [y/N] ").strip().lower() in ("y", "yes"):
            from .oauth import authorize_microsoft
            ok, msg = authorize_microsoft()
            print(f"  {'[OK]  ' if ok else '[warn]'} {msg}")


def _print_doctor_summary() -> None:
    """A compact readiness table from doctor, so setup ends by confirming reality."""
    try:
        from .doctor import run_doctor
        report = run_doctor()
    except Exception as exc:
        print(f"  (couldn't run readiness check: {exc})")
        return
    checks = report.get("checks", {})
    for name, c in checks.items():
        if not isinstance(c, dict):
            continue
        mark = "[OK]  " if c.get("ok") else "[--]  "
        print(f"  {mark} {name}: {c.get('detail', '')}")
