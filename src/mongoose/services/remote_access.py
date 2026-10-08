"""Public-facing remote access: the canonical URL and the chain validator.

M runs Mongoose deliberately internet-facing at a single canonical hostname
(e.g. https://yourname.duckdns.org, router forwarding external 443 to Mongoose's
port). Everything a remote client needs travels through that one name; no raw
ports in links or QR codes.

This module gives that decision first-class support:

  public_base_url()   The canonical https URL. MONGOOSE_PUBLIC_URL wins; otherwise
                      it is derived from the configured DuckDNS hostname.

  remote_readiness()  The end-to-end chain check: hardening flags, token posture,
                      bind address, DDNS freshness, DNS-vs-public-IP agreement,
                      Let's Encrypt certificate health, and an optional outside-in
                      probe of https://<host>/health. Each link reports separately,
                      because "it doesn't work from the pub" is otherwise
                      undiagnosable.

Honest boundaries: the router's port-forward rule cannot be inspected from inside
the LAN. The outside-in probe exercises it only if the router hairpins NAT; when
that probe fails, the readiness text says which of the two explanations apply
rather than guessing.
"""
from __future__ import annotations

import os
import socket
from typing import Any


def _effective_env() -> dict[str, str]:
    try:
        from ..setup_wizard import effective_env
        return dict(effective_env())
    except Exception:
        return dict(os.environ)


def _saved_env() -> dict[str, str]:
    try:
        from ..setup_wizard import _read_env
        return dict(_read_env())
    except Exception:
        return {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def public_base_url(env: dict | None = None) -> str | None:
    """The single canonical public URL, no trailing slash, or None when not configured.

    Reads the saved setup .env as well as the current process environment when no explicit env
    is passed: the wizard already writes DuckDNS values there, and readiness must not call them
    "unconfigured" just because this process has not restarted since setup saved the file.
    MONGOOSE_PUBLIC_URL takes precedence; otherwise derived from the DuckDNS host."""
    if env is None:
        env = _effective_env()
        for key, value in _saved_env().items():
            if value and key not in env:
                env[key] = value
    explicit = (env.get("MONGOOSE_PUBLIC_URL") or os.environ.get("MONGOOSE_PUBLIC_URL", "")).strip().rstrip("/")
    if explicit:
        explicit = explicit if "://" in explicit else f"https://{explicit}"
        # Re-check a stale explicit http:// override against whether HTTPS is actually
        # available now; "explicit wins" should not also mean "explicit is frozen forever
        # at whatever scheme it was set to before HTTPS existed".
        try:
            from ..ddns import upgrade_stale_public_scheme
            explicit = upgrade_stale_public_scheme(explicit, env)
        except Exception:
            pass
        return explicit
    try:
        from ..ddns import public_url
        base = public_url(env)
        if base:
            return str(base).rstrip("/")
    except Exception:
        pass
    try:
        from ..acme_tls import _duckdns_host
        host = _duckdns_host()
    except Exception:
        host = None
    return f"https://{host}" if host else None


def public_hostname(env: dict | None = None) -> str | None:
    url = public_base_url(env)
    if not url:
        return None
    return url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]


def is_public_facing(env: dict | None = None) -> bool:
    env = env if env is not None else _effective_env()
    return _truthy(env.get("MONGOOSE_PUBLIC_FACING") or os.environ.get("MONGOOSE_PUBLIC_FACING", ""))


def _resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET)
        return sorted({i[4][0] for i in infos})
    except Exception:
        return []


def remote_readiness(*, probe_outside: bool = True, timeout: float = 4.0) -> dict[str, Any]:
    """Validate every link in the public-access chain. Read-only; network calls are
    each individually wrapped so an offline run degrades to 'unverifiable' not error."""
    from ..settings import get_settings
    checks: list[dict[str, Any]] = []

    def add(label: str, ok: bool | None, detail: str) -> None:
        checks.append({"label": label, "ok": ok, "detail": detail})

    s = get_settings()
    env = _effective_env()
    saved_env = _saved_env()
    for key, value in saved_env.items():
        if value and key not in env:
            env[key] = value
    url = public_base_url(env)
    host = public_hostname(env)

    add("Canonical public URL", bool(url),
        url or "set MONGOOSE_PUBLIC_URL or configure the DuckDNS provider")

    public_facing = is_public_facing(env)
    add("Public-facing hardening flag", public_facing,
        "MONGOOSE_PUBLIC_FACING=true (first-run surfaces closed to the internet)"
        if public_facing else
        "set MONGOOSE_PUBLIC_FACING=true to close first-run surfaces (/welcome, /install, /tutorial)")

    live_host = str(getattr(s, "host", "localhost"))
    saved_host = str(saved_env.get("MONGOOSE_HOST") or env.get("MONGOOSE_HOST") or live_host)
    live_bound = live_host in ("0.0.0.0", "::")
    saved_bound = saved_host in ("0.0.0.0", "::")
    needs_restart = saved_bound and not live_bound
    add("Server binds the LAN", live_bound,
        f"live MONGOOSE_HOST={live_host}; saved MONGOOSE_HOST={saved_host}"
        + (" (restart needed to pick up the saved value)" if needs_restart
           else ("" if live_bound else " (needs 0.0.0.0)")))

    saved_allow = _truthy(saved_env.get("MONGOOSE_ALLOW_REMOTE") or env.get("MONGOOSE_ALLOW_REMOTE"))
    saved_token = bool(saved_env.get("MONGOOSE_API_TOKEN") or env.get("MONGOOSE_API_TOKEN") or "")
    live_allow = bool(getattr(s, "allow_remote", False))
    live_token = bool(getattr(s, "api_token", ""))
    token_ok = (live_allow and live_token) or (saved_allow and saved_token)
    add("Remote auth posture", token_ok,
        "MONGOOSE_ALLOW_REMOTE + MONGOOSE_API_TOKEN set" if token_ok else
        "set MONGOOSE_ALLOW_REMOTE=true and a strong MONGOOSE_API_TOKEN")

    # DDNS freshness + DNS agreement. Local readiness screens call this with probe_outside=False
    # while rendering the cockpit; in that mode it must stay instant and never block the UI on a
    # public-IP echo or a DNS lookup (a slow or captive network turned the "quick" readiness panel
    # into a hang). Full network validation still runs when probe_outside=True.
    dns_ips: list[str] = []
    public_ip: str | None = None
    try:
        from ..ddns import detect_public_ip, status
        st = status(env)
        add("DDNS provider", bool(st.get("provider") and st.get("enabled")),
            f"{st.get('provider') or 'none'} ({'enabled' if st.get('enabled') else 'disabled'})")
        if probe_outside:
            try:
                public_ip = detect_public_ip(timeout=timeout)
            except Exception:
                public_ip = None
    except Exception as exc:
        add("DDNS provider", False, f"ddns module error: {exc}")

    if host:
        if not probe_outside:
            add("DNS resolves", None, f"{host}: skipped for this quick readiness pass")
            dns_ips = []
        elif not (dns_ips := _resolve(host)):
            add("DNS resolves", False, f"{host} did not resolve (or no network from here)")
        elif public_ip is None:
            add("DNS resolves", None, f"{host} -> {', '.join(dns_ips)}; public IP undetectable from here")
        else:
            agree = public_ip in dns_ips
            add("DNS matches public IP", agree,
                f"{host} -> {', '.join(dns_ips)}; this network's public IP is {public_ip}"
                + ("" if agree else " (stale DDNS record, or you are on a different network)"))
            if agree:
                # The trap that shows up as "Mongoose is offline" on a phone that is sitting
                # ten feet from the Mac: the public name resolves to the router's WAN side, and
                # devices ON the LAN can only loop back through it if the router hairpins NAT.
                # Many don't. The robust fix is a local DNS rewrite so the name resolves to the
                # Mac's LAN address inside the house and to the public IP outside.
                lan = _lan_ip()
                add("LAN access to the public name", None,
                    f"relies on router NAT hairpin OR a local DNS rewrite. Recommended: in AdGuard "
                    f"Home add a DNS rewrite {host} -> {lan or 'the Mac LAN IP'} so the same URL "
                    f"works on and off wifi; then this cannot fail from inside the house.")

    # Certificate health (Let's Encrypt via DNS-01)
    try:
        from ..acme_tls import certificate_status
        cs = certificate_status()
        if cs.get("present"):
            days = cs.get("days_remaining")
            covers = cs.get("hostnames") or cs.get("covers") or []
            host_ok = (host in covers) if (host and covers) else None
            detail = f"expires in {days} days" if days is not None else "present"
            if host_ok is False:
                detail += f"; cert does not cover {host}"
            add("Let's Encrypt certificate", (days or 0) > 14 and host_ok is not False, detail)
        else:
            add("Let's Encrypt certificate", False,
                "no certificate; run: mongoose tls lets-encrypt (DNS-01, no inbound port needed)")
    except Exception as exc:
        add("Let's Encrypt certificate", None, f"status unreadable: {exc}")

    # Outside-in probe: exercises DNS + forward + TLS + Mongoose in one shot when the
    # router hairpins; when it fails from inside the LAN, either the forward rule is
    # missing OR the router simply doesn't hairpin, and only a phone off wifi can tell.
    if probe_outside and url:
        try:
            import httpx
            r = httpx.get(f"{url}/health", timeout=timeout)
            add("Outside-in probe", r.status_code == 200,
                f"GET {url}/health -> {r.status_code}")
        except Exception as exc:
            add("Outside-in probe", None,
                f"unreachable from inside the LAN ({type(exc).__name__}): either the router's "
                f"443 forward is missing, or it does not hairpin NAT; verify once from a phone on mobile data")

    hard_fails = [c for c in checks if c["ok"] is False]
    return {
        "ok": not hard_fails,
        "public_url": url,
        "checks": checks,
        "failing": [c["label"] for c in hard_fails],
    }


def _lan_ip() -> str | None:
    try:
        import socket as _s
        s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None
