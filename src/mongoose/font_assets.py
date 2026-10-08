from __future__ import annotations

"""Runtime provisioning for optional display fonts.

Mongoose ships the core UI fonts inside the package, but the guest invite wants
Rajdhani specifically. We deliberately do NOT vendor that binary in every patch:
instead the owner install can fetch the open-source webfont once, cache it under
the persistent Mongoose data directory, and serve it locally thereafter. If the network is unavailable,
the CSS falls back to any system-installed Rajdhani and then to Inter.
"""

import hashlib
import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

RAJDHANI_FAMILY = "Rajdhani"
# Two sources, tried in order. Bunny Fonts is a drop-in, API-compatible Google Fonts mirror built
# specifically to be a privacy-respecting alternative (no visitor IP/UA sent to Google on every
# page load); it also happens to be far less likely to sit on a DNS blocklist than
# fonts.googleapis.com/fonts.gstatic.com, which several popular ad/tracker blocklists include
# outright, including ones commonly loaded into self-hosted DNS filters. Google Fonts is kept as
# an automatic second attempt in case Bunny is ever unreachable, not the other way around.
RAJDHANI_CSS_URLS = (
    "https://fonts.bunny.net/css2?family=rajdhani:500,600,700",
    "https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;600;700&display=swap",
)
RAJDHANI_USER_AGENT = (
    "Mozilla/5.0 (compatible; Mongoose/1.1; +https://local.mongoose.invalid/fonts)"
)

# Not hardcoded to one CDN host: Bunny and Google serve the actual woff2 files from different
# domains, and the regex just needs to find whatever https URL the chosen source's CSS points at.
_FONT_URL_RE = re.compile(r"url\((https://[^)]+?\.woff2)\)")
_THREAD_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_LAST_ATTEMPT = 0.0
_MIN_RETRY_SECONDS = 300.0


def _cache_root() -> Path:
    from .settings import get_settings

    return Path(getattr(get_settings(), "data_dir", Path("data"))) / "font-cache" / "rajdhani"


def _css_path() -> Path:
    return _cache_root() / "rajdhani.css"


def _manifest_path() -> Path:
    return _cache_root() / "manifest.json"


def _mode() -> str:
    return os.getenv("MONGOOSE_FONT_PROVISIONING", "auto").strip().lower()


def _remote_enabled() -> bool:
    return _mode() not in {"0", "false", "no", "off", "disabled", "manual"}


def _fallback_css(reason: str = "not provisioned yet") -> str:
    # local() keeps the right face for users who already have Rajdhani installed;
    # there is intentionally no CDN fallback here because Mongoose should not leak
    # every guest invite page load to a third-party font host.
    return (
        "/* Rajdhani fallback: %s. Run font provisioning or let an owner-created "
        "guest invite warm the cache. */\n"
        "@font-face{font-family:'Rajdhani';font-style:normal;font-weight:500 700;"
        "font-display:swap;src:local('Rajdhani'),local('Rajdhani SemiBold'),local('Rajdhani Bold');}\n"
    ) % str(reason).replace("*/", "")


def cached_rajdhani_css() -> str | None:
    path = _css_path()
    if not path.exists():
        return None
    try:
        css = path.read_text(encoding="utf-8")
    except Exception:
        return None
    if "font-family" not in css or "Rajdhani" not in css:
        return None
    # Avoid serving a stale CSS file whose local font files were deleted.
    names = re.findall(r"/static/fonts/provisioned/rajdhani/([A-Za-z0-9._-]+\.woff2)", css)
    if not names:
        return None
    root = _cache_root()
    if not all((root / name).is_file() for name in names):
        return None
    return css


def rajdhani_css(*, kick: bool = True) -> tuple[str, dict[str, Any]]:
    """Return local Rajdhani CSS if cached, otherwise a quick local() fallback.

    This endpoint must stay fast: the browser asks for it while rendering a guest
    invite. Network work is kicked to a background thread instead of blocking the
    first paint.
    """
    cached = cached_rajdhani_css()
    if cached:
        return cached, {"ok": True, "source": "cache", "family": RAJDHANI_FAMILY}
    if kick:
        kick_rajdhani_provisioning()
    return _fallback_css("cache missing"), {"ok": False, "source": "fallback", "family": RAJDHANI_FAMILY}


def resolve_rajdhani_file(name: str) -> Path | None:
    safe = Path(str(name or "")).name
    if safe != name or not safe.endswith(".woff2"):
        return None
    path = _cache_root() / safe
    return path if path.is_file() else None


def _request(url: str, *, timeout: float) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": RAJDHANI_USER_AGENT})


def _fetch_text(url: str, *, timeout: float) -> str:
    with urllib.request.urlopen(_request(url, timeout=timeout), timeout=timeout) as r:  # nosec - fixed trusted URL
        data = r.read(256_000)
    return data.decode("utf-8", errors="replace")


def _fetch_bytes(url: str, *, timeout: float) -> bytes:
    with urllib.request.urlopen(_request(url, timeout=timeout), timeout=timeout) as r:  # nosec - fixed Google Fonts URL parsed from CSS
        return r.read(2_000_000)


def _font_name(url: str, index: int) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"rajdhani-{index:02d}-{digest}.woff2"


def provision_rajdhani(*, timeout: float = 12.0, force: bool = False) -> dict[str, Any]:
    """Download Rajdhani CSS + woff2 files once and rewrite them to local URLs.

    Returns a structured status for Cockpit/doctor/tests. The function is safe to
    call repeatedly; it is a no-op once the cache is valid unless force=True.
    """
    if not force:
        cached = cached_rajdhani_css()
        if cached:
            return {"ok": True, "source": "cache", "family": RAJDHANI_FAMILY,
                    "css_path": str(_css_path())}
    if not _remote_enabled() and not force:
        return {"ok": False, "source": "disabled", "family": RAJDHANI_FAMILY,
                "message": "MONGOOSE_FONT_PROVISIONING is not auto-enabled."}

    start = time.monotonic()
    root = _cache_root()
    root.mkdir(parents=True, exist_ok=True)
    attempts: list[dict[str, str]] = []
    for css_url in RAJDHANI_CSS_URLS:
        elapsed = time.monotonic() - start
        remaining_total = max(2.0, float(timeout) - elapsed)
        if remaining_total <= 1.0 and attempts:
            break  # already spent the budget on a prior source; do not start a doomed one
        try:
            css = _fetch_text(css_url, timeout=max(2.0, min(remaining_total, 12.0)))
            urls = list(dict.fromkeys(_FONT_URL_RE.findall(css)))
            if not urls:
                attempts.append({"source_css": css_url, "error": "no_font_urls"})
                continue
            mapping: dict[str, str] = {}
            for i, url in enumerate(urls, start=1):
                elapsed = time.monotonic() - start
                remaining = max(2.0, float(timeout) - elapsed)
                name = _font_name(url, i)
                path = root / name
                if not path.exists() or force:
                    data = _fetch_bytes(url, timeout=min(remaining, 8.0))
                    tmp = path.with_suffix(".woff2.tmp")
                    tmp.write_bytes(data)
                    tmp.replace(path)
                mapping[url] = name
            local_css = css
            for remote, name in mapping.items():
                local_css = local_css.replace(remote, f"/static/fonts/provisioned/rajdhani/{name}")
            header = (
                "/* Rajdhani provisioned by Mongoose. Source CSS: "
                f"{css_url}. Served locally after first fetch. */\n"
            )
            _css_path().write_text(header + local_css, encoding="utf-8")
            weights = sorted(set(re.findall(r"font-weight:\s*(\d+)", css)))
            _manifest_path().write_text(json.dumps({
                "family": RAJDHANI_FAMILY,
                "source_css": css_url,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "files": list(mapping.values()),
                "weights": weights,
            }, indent=2), encoding="utf-8")
            return {"ok": True, "source": "remote", "family": RAJDHANI_FAMILY,
                    "source_css": css_url, "files": len(mapping), "weights": weights,
                    "css_path": str(_css_path())}
        except Exception as exc:
            attempts.append({"source_css": css_url, "error": type(exc).__name__, "message": str(exc)})
            continue
    # Every source failed. Report the LAST attempt's detail (most informative for a genuine
    # network-down case) plus how many sources were tried, so a caller never has to guess
    # whether this was a one-off blip on a single host or everything actually being unreachable.
    last = attempts[-1] if attempts else {"error": "no_attempts"}
    return {"ok": False, "source": "remote", "family": RAJDHANI_FAMILY, "sources_tried": len(attempts),
            "error": last.get("error", "unknown"), "message": last.get("message", "")}


def kick_rajdhani_provisioning(*, timeout: float = 12.0) -> dict[str, Any]:
    """Start best-effort Rajdhani provisioning in the background.

    Owner invite creation calls this so the font is likely cached before a guest
    opens their URL. It never blocks invite generation and rate-limits failures.
    """
    global _THREAD, _LAST_ATTEMPT
    if cached_rajdhani_css():
        return {"ok": True, "source": "cache", "family": RAJDHANI_FAMILY}
    if not _remote_enabled():
        return {"ok": False, "source": "disabled", "family": RAJDHANI_FAMILY}
    now = time.monotonic()
    with _THREAD_LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return {"ok": False, "source": "background", "family": RAJDHANI_FAMILY,
                    "status": "already_running"}
        if now - _LAST_ATTEMPT < _MIN_RETRY_SECONDS:
            return {"ok": False, "source": "background", "family": RAJDHANI_FAMILY,
                    "status": "recently_attempted"}
        _LAST_ATTEMPT = now

        def _run() -> None:
            try:
                provision_rajdhani(timeout=timeout)
            except Exception:
                pass

        _THREAD = threading.Thread(target=_run, name="mongoose-rajdhani-font-provisioner", daemon=True)
        _THREAD.start()
    return {"ok": False, "source": "background", "family": RAJDHANI_FAMILY,
            "status": "started"}
