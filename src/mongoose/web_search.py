from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse, urlencode

# SearXNG web search client.
#
# Best-practice contract (per SearXNG docs):
#   - Query GET /search with format=json (an unset format returns 403; many public
#     instances disable JSON entirely, so we surface that clearly).
#   - Each result has title / url / content / engine; we map those onto Mongoose's
#     grounded source-record shape (title, kind, where, open, snippet) so web sources
#     are interchangeable with local ones in the research worker.
#
# Design choices (the owner's):
#   - OPT-IN: disabled unless MONGOOSE_SEARXNG_URL is set. No default public instance,
#     a default would leak your queries to a third party and break local-first.
#   - SELF-HOSTED LAN ALLOWED: a private/LAN address is permitted BECAUSE you set it
#     explicitly. But to stay SSRF-safe, ONLY the host you configured is allowed; the
#     resolved address must match the configured host, so a crafted query/redirect
#     can't pivot to another internal address. Public hosts must be https.
#   - SNIPPETS ONLY: we never fetch result pages. Lighter, faster, and it avoids a
#     whole class of SSRF/scraping concerns. SearXNG's content field is the snippet.


class SearxngError(Exception):
    """SearXNG is misconfigured, unreachable, or rejected the request."""


def _is_private_host(host: str) -> bool:
    """True if host resolves to a private/loopback/link-local address (i.e. a LAN
    instance). Used to decide whether https is required (public) vs allowed-by-optin
    (private)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for *_, sockaddr in infos:
        ip = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            return True
    return False


def _validate_base_url(base_url: str) -> str:
    """Validate the configured SearXNG base URL. Returns a normalised base or raises.
    Public hosts must be https. Private/LAN hosts are allowed over http BECAUSE the
    user opted in by configuring them explicitly."""
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SearxngError(f"invalid SearXNG URL: {base_url!r} (need http(s)://host)")
    host = parsed.hostname
    if not _is_private_host(host) and parsed.scheme != "https":
        # public instance over plaintext: refuse (queries would be in the clear)
        raise SearxngError("public SearXNG instances must use https; "
                           f"got {base_url!r}")
    return base_url.rstrip("/")


def configured_url() -> str | None:
    """The configured SearXNG base URL, or None if web search is disabled (opt-in)."""
    from .settings import get_settings
    return getattr(get_settings(), "searxng_url", "") or None


def _fallback_enabled() -> bool:
    """Whether the native (no-container) DuckDuckGo fallback may be used when SearXNG is not reachable.
    ON by default so web search keeps working when Docker is off (M sheds Docker for RAM). It is a real
    privacy trade (the query goes to DuckDuckGo, not a self-hosted instance), so it has a kill switch:
    set MONGOOSE_WEB_SEARCH_FALLBACK=false to keep web search strictly SearXNG-only."""
    import os
    return str(os.getenv("MONGOOSE_WEB_SEARCH_FALLBACK", "true")).strip().lower() not in ("0", "false", "no", "off")


def is_enabled() -> bool:
    """Web search is available if a SearXNG instance is configured OR the native fallback is allowed."""
    return configured_url() is not None or _fallback_enabled()


async def _searxng_search(base: str, query: str, limit: int, time_range: str | None) -> list[dict[str, Any]]:
    """Query the configured SearXNG instance. Raises SearxngError on any failure so the caller can fall
    back to the native path."""
    import httpx
    base = _validate_base_url(base)
    params = {"q": query, "format": "json", "categories": "general", "safesearch": "1"}
    if time_range in ("day", "month", "year"):
        params["time_range"] = time_range
    url = f"{base}/search?{urlencode(params)}"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as c:
            r = await c.get(url, headers={"Accept": "application/json"})
    except Exception as exc:
        raise SearxngError(f"SearXNG unreachable at {base}: {exc}") from exc
    if r.status_code == 403:
        raise SearxngError("SearXNG returned 403: enable the json format in "
                           "settings.yml (search: formats: [html, json])")
    if r.status_code != 200:
        raise SearxngError(f"SearXNG returned HTTP {r.status_code}")
    try:
        data = r.json()
    except Exception as exc:
        raise SearxngError(f"SearXNG returned non-JSON: {exc}") from exc
    out: list[dict[str, Any]] = []
    for item in (data.get("results") or [])[:limit]:
        u = item.get("url") or ""
        out.append({"kind": "web", "title": item.get("title") or u or "untitled",
                    "where": u, "open": u, "snippet": (item.get("content") or "")[:240],
                    "engine": item.get("engine") or "searxng"})
    return out


async def _duckduckgo_search(query: str, limit: int, time_range: str | None) -> list[dict[str, Any]]:
    """Native, no-container fallback: DuckDuckGo's HTML endpoint (no API key, no Docker). Snippets only,
    same record shape as the SearXNG path. Kept dependency-free (regex parse, not bs4)."""
    import html as _html
    import re
    import httpx
    from urllib.parse import parse_qs, unquote
    data = {"q": query, "kl": "uk-en"}
    df = {"day": "d", "month": "m", "year": "y"}.get(time_range or "")
    if df:
        data["df"] = df
    headers = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
               "Accept": "text/html", "Content-Type": "application/x-www-form-urlencoded"}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.post("https://html.duckduckgo.com/html/", data=data, headers=headers)
    except Exception as exc:
        raise SearxngError(f"DuckDuckGo fallback unreachable: {exc}") from exc
    if r.status_code != 200:
        raise SearxngError(f"DuckDuckGo fallback returned HTTP {r.status_code}")

    def _clean(s: str) -> str:
        return _html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()

    def _unwrap(href: str) -> str:
        # DuckDuckGo wraps outbound links as //duckduckgo.com/l/?uddg=<encoded-url>
        if "uddg=" in href:
            q = parse_qs(urlparse(("https:" + href) if href.startswith("//") else href).query)
            if q.get("uddg"):
                return unquote(q["uddg"][0])
        return ("https:" + href) if href.startswith("//") else href

    body = r.text
    anchors = re.findall(r'<a\b[^>]*class="result__a"[^>]*>.*?</a>', body, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', body, re.S)
    href_re = re.compile(r'href="([^"]+)"')
    out: list[dict[str, Any]] = []
    for i, a in enumerate(anchors):
        if len(out) >= limit:
            break
        m = href_re.search(a)
        if not m:
            continue
        u = _unwrap(m.group(1))
        # DuckDuckGo injects sponsored results (ad redirector y.js / ad_domain). Drop them so a paid
        # placement never poses as a real source.
        if "duckduckgo.com/y.js" in u or "ad_domain=" in u or "ad_provider=" in u:
            continue
        title = _clean(re.sub(r'^<a\b[^>]*>', "", a).rsplit("</a>", 1)[0])
        snip = _clean(snippets[i]) if i < len(snippets) else ""
        out.append({"kind": "web", "title": title or u or "untitled", "where": u, "open": u,
                    "snippet": snip[:240], "engine": "duckduckgo"})
    return out


async def search_web(query: str, *, limit: int = 5,
                     time_range: str | None = None) -> list[dict[str, Any]]:
    """Return grounded source records: {kind:'web', title, where(=url), open(=url), snippet, engine}.
    Snippets only, no page fetching. Tries the configured SearXNG instance first (local-first, private);
    if it is not configured or not reachable, falls back to the native DuckDuckGo path so web search keeps
    working with Docker off. Raises SearxngError only when NO backend answers, so callers degrade cleanly."""
    base = configured_url()
    errors: list[str] = []
    if base:
        try:
            res = await _searxng_search(base, query, limit, time_range)
            if res:
                return res
        except SearxngError as exc:
            errors.append(str(exc))
    if _fallback_enabled():
        try:
            res = await _duckduckgo_search(query, limit, time_range)
            if res:
                return res
        except SearxngError as exc:
            errors.append(str(exc))
    if not base and not _fallback_enabled():
        raise SearxngError("web search disabled (set MONGOOSE_SEARXNG_URL, or allow the "
                           "MONGOOSE_WEB_SEARCH_FALLBACK)")
    raise SearxngError("no web search backend returned results"
                       + (f" ({'; '.join(errors)})" if errors else ""))
