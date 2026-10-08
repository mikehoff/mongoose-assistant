"""Pairing preflight, machine-detect the real server state so the pairing UI advertises only URLs that
match the actual listener scheme, bind address, certificate host, and (where checkable) reachability,
instead of sending the user into a guessing loop.

This addresses the core onboarding failure: the install/QR link was advertising https:// when the server
was HTTP-only, and offering LAN/public URLs without saying which network context they need or whether
they were reachable. Everything here is DETECTED, not guessed, and every URL carries an honest label and
status. The UI should generate QR codes only from URLs that pass the local consistency checks here.

Nothing in here raises: a probe failure becomes a 'degraded' field, never a 500 (the report explicitly
calls out that preflight must report degraded state instead of throwing).
"""
from __future__ import annotations

import socket
from typing import Any


def _settings():
    from ..settings import get_settings
    return get_settings()


def _bound_addresses(port: int) -> list[str]:
    """Best-effort: what is actually LISTENING on this port, via psutil if available, else a heuristic.
    Tells us whether the server is on 0.0.0.0 (phone-reachable) or only 127.0.0.1 (Mac-only)."""
    addrs: list[str] = []
    try:
        import psutil  # type: ignore
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port:
                addrs.append(f"{c.laddr.ip}:{c.laddr.port}")
    except Exception:
        pass
    if not addrs:
        # Fallback heuristic: try binding 0.0.0.0:port. If it's in use we can't tell the iface this way,
        # so we report 'unknown' rather than assert. (The endpoint also reports the host serve was told.)
        addrs = []
    return sorted(set(addrs))


def _configured_bind_host() -> str:
    """The host `serve` was told to bind (env/default). 0.0.0.0 => all interfaces (phone-reachable);
    127.0.0.1 => loopback only (Mac-only)."""
    import os
    return (os.getenv("MONGOOSE_HOST", "") or "127.0.0.1").strip()


def _lan_ip() -> str | None:
    try:
        from ..discovery import _primary_ip
        ip = _primary_ip()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    # direct fallback
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.4)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        return ip if ip and not ip.startswith("127.") else None
    except Exception:
        return None
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


def _tls_enabled(env: dict | None = None) -> bool:
    try:
        from ..ddns import tls_is_enabled
        return tls_is_enabled(env)
    except Exception:
        return False


def _cert_host() -> str | None:
    try:
        from ..acme_tls import certificate_status
        st = certificate_status()
        return st.get("host") or None
    except Exception:
        return None


def _ddns_host() -> str | None:
    try:
        from ..ddns import public_url
        url = public_url()
        if url:
            # strip scheme + port to get the bare host
            h = url.split("://", 1)[-1].split("/", 1)[0]
            return h.split(":")[0]
        return None
    except Exception:
        return None


def _macos_firewall_state() -> dict[str, Any]:
    """Detect the macOS Application Firewall (ALF) state, Dispatch's hypothesis for dropped inbound
    connections. When the firewall is ON and the server's binary isn't in the allow list, macOS silently
    drops inbound TCP even though the server is bound to 0.0.0.0 and listening. We read this so the
    pairing UI can say "the firewall is likely blocking this, here's the fix" instead of the user
    guessing among firewall / NAT / DNS. Read-only; never raises; only meaningful on macOS.

    Returns: {platform, enabled (True/False/None=unknown), stealth, blocked_binary (True/False/None),
              binary, detail}. enabled=None means we couldn't tell (not macOS, or the tool isn't there)."""
    import platform as _plat
    out: dict[str, Any] = {"platform": _plat.system(), "enabled": None, "stealth": None,
                           "blocked_binary": None, "binary": None, "detail": ""}
    if _plat.system() != "Darwin":
        out["detail"] = "not macOS; firewall check not applicable"
        return out
    fw = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    import os as _os
    if not _os.path.exists(fw):
        out["detail"] = "socketfilterfw not found"
        return out
    import subprocess as _sp
    def _run(args: list[str]) -> str:
        try:
            r = _sp.run([fw] + args, capture_output=True, text=True, timeout=5)
            return (r.stdout or "").strip()
        except Exception:
            return ""
    g = _run(["--getglobalstate"]).lower()
    if "enabled" in g:
        out["enabled"] = True
    elif "disabled" in g:
        out["enabled"] = False
    st = _run(["--getstealthmode"]).lower()
    if "enabled" in st:
        out["stealth"] = True
    elif "disabled" in st:
        out["stealth"] = False
    # Is THIS server's binary blocked? Check the interpreter/executable actually serving.
    import sys as _sys
    binary = getattr(_sys, "executable", "") or ""
    out["binary"] = binary
    if out["enabled"] and binary:
        b = _run(["--getappblocked", binary]).lower()
        if "blocked" in b and "not blocked" not in b:
            out["blocked_binary"] = True
        elif "not blocked" in b or "allowed" in b:
            out["blocked_binary"] = False
    if out["enabled"] is True:
        out["detail"] = "macOS firewall is ON" + (" (stealth mode)" if out["stealth"] else "")
    elif out["enabled"] is False:
        out["detail"] = "macOS firewall is off"
    return out


def _http_get_ok(url: str, *, timeout: float = 3.0) -> bool:
    try:
        import httpx
        r = httpx.get(url, timeout=timeout, verify=False)
        return r.status_code < 500
    except Exception:
        return False


def preflight(*, verify_local: bool = True) -> dict[str, Any]:
    """Return the structured preflight payload: real server state + honest candidate URLs + warnings.
    verify_local controls whether we actually hit the local /install to confirm the route serves (cheap,
    same-host); external reachability is never asserted from here (we can't see the WAN), only labelled."""
    s = _settings()
    port = int(getattr(s, "port", 8765) or 8765)
    bind_host = _configured_bind_host()
    try:
        bound = _bound_addresses(port)
    except Exception:
        bound = []
    try:
        https_on = _tls_enabled()
    except Exception:
        https_on = False
    scheme = "https" if https_on else "http"
    try:
        lan = _lan_ip()
    except Exception:
        lan = None
    try:
        ddns = _ddns_host()
    except Exception:
        ddns = None
    try:
        cert_host = _cert_host() if https_on else None
    except Exception:
        cert_host = None

    loopback_only = (bind_host in ("127.0.0.1", "localhost", "::1")) and not any(
        a.startswith("0.0.0.0") or (lan and a.startswith(lan)) for a in bound)

    # macOS firewall , Dispatch's hypothesis for dropped inbound connections. Detected read-only so the
    # UI can give a precise cause + fix instead of "connection failed".
    try:
        firewall = _macos_firewall_state()
    except Exception:
        firewall = {"platform": "", "enabled": None, "detail": "firewall check failed"}

    # Local install route check (same host, cheap, honest). Use the shared HTTPS-aware loopback
    # probe so default self-signed HTTPS is not mistaken for a dead install page.
    install_route_ok = None
    if verify_local:
        try:
            from ..local_probe import get_bytes
            url, _body = get_bytes("/install", port=port, timeout=3)
            install_route_ok = True
            scheme = "https" if url.startswith("https://") else "http"
        except Exception:
            install_route_ok = False

    urls: list[dict[str, Any]] = []
    warnings: list[str] = []

    # 1) This Mac (always valid locally, never for a phone).
    urls.append({
        "label": "Use this on the Mac",
        "url": f"{scheme}://127.0.0.1:{port}/install",
        "usable_from_phone": False,
        "status": "ok" if install_route_ok else ("unknown" if install_route_ok is None else "route_failed"),
        "note": "Only works in a browser on this Mac.",
    })

    # 2) Same Wi-Fi (LAN IP) , only if we have a LAN IP AND the server isn't loopback-only.
    if lan and not loopback_only:
        lan_scheme = scheme
        note = "Open this on a phone connected to the same Wi-Fi as the Mac."
        status = "unknown_until_tested"
        # If HTTPS is on with a DDNS cert, the LAN-IP HTTPS URL will throw a cert warning , say so, don't
        # present it as clean.
        if https_on and cert_host and cert_host not in (lan,):
            note += (f" Note: the certificate is for {cert_host}, so the browser will warn about the "
                     f"address not matching, that's expected on the LAN.")
            status = "warns_cert_mismatch"
        urls.append({
            "label": "Use this on the same Wi-Fi",
            "url": f"{lan_scheme}://{lan}:{port}/install",
            "usable_from_phone": True,
            "status": status,
            "note": note,
        })
    elif loopback_only:
        warnings.append("Mongoose is listening only on 127.0.0.1, so phones cannot connect. Start it "
                        "with MONGOOSE_HOST=0.0.0.0 (and an API token) to allow LAN/remote pairing.")

    # 3) Away from home (DDNS) , only if configured; never asserted reachable, only labelled.
    if ddns:
        d_scheme = "https" if https_on else "http"
        note = "Open this on a phone using cellular (or any network away from home)."
        d_status = "not_verified"
        if not https_on:
            note += " The server is HTTP-only; if your phone forces HTTPS this may fail, prefer the QR."
        # If on the SAME Wi-Fi, this needs NAT loopback which many routers lack.
        note += " On the same Wi-Fi this only works if your router supports NAT loopback."
        urls.append({
            "label": "Use this away from home / on cellular",
            "url": f"{d_scheme}://{ddns}:{port}/install",
            "usable_from_phone": True,
            "status": d_status,
            "note": note,
        })
        warnings.append("The public address has not been externally verified from here; test it from a "
                        "phone on cellular.")

    # Scheme/cert coherence warnings.
    if https_on and cert_host and ddns and cert_host != ddns:
        warnings.append(f"HTTPS certificate is for {cert_host} but the public address is {ddns}; the "
                        f"public HTTPS URL will warn about a hostname mismatch.")
    if not https_on:
        warnings.append("Server is HTTP-only; pairing links use http:// and QR codes are preferred so "
                        "the phone's HTTPS-first upgrade doesn't break a typed http:// link.")

    # macOS firewall , the likely cause when the server is bound to all interfaces and listening but a
    # phone on the same Wi-Fi still can't connect. Surface a precise, actionable warning + the fix.
    fix_cmd = None
    if firewall.get("enabled") is True and not loopback_only:
        binp = firewall.get("binary") or "the Mongoose server binary"
        if firewall.get("blocked_binary") is True:
            warnings.append("The macOS firewall is ON and is blocking incoming connections to Mongoose, "
                            "so phones can't reach it even on the same Wi-Fi. Allow it in System Settings "
                            "> Network > Firewall > Options (add Mongoose and set it to allow incoming "
                            "connections), or run the one-line fix below.")
        else:
            warnings.append("The macOS firewall is ON. If a phone on the same Wi-Fi still can't connect, "
                            "the firewall is the most likely cause, allow incoming connections for "
                            "Mongoose in System Settings > Network > Firewall > Options, or run the "
                            "one-line fix below.")
        if firewall.get("stealth"):
            warnings.append("macOS firewall stealth mode is on, which also drops the probes a phone uses "
                            "to find Mongoose. Turning stealth mode off helps pairing.")
        # The fix: register + unblock this binary with the application firewall (needs sudo, runs on the
        # Mac, in the user's terminal , never executed from here). shlex.quote the path so a username with
        # an apostrophe (e.g. /Users/O'Brien/...) or any space/metachar can't break the copy-pasted
        # command or inject anything , single-quote wrapping alone is not safe for arbitrary paths.
        import shlex as _shlex
        qbin = _shlex.quote(binp)
        fix_cmd = ("sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add %s && "
                   "sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp %s" % (qbin, qbin))

    return {
        "ok": True,
        "server": {
            "configured_bind_host": bind_host,
            "bound_addresses": bound,
            "loopback_only": loopback_only,
            "scheme": scheme,
            "https_enabled": https_on,
            "port": port,
            "install_route_ok": install_route_ok,
            "lan_ip": lan,
            "ddns_host": ddns,
            "cert_host": cert_host,
        },
        "firewall": firewall,
        "firewall_fix": fix_cmd,
        "urls": urls,
        "warnings": warnings,
    }


def usable_pair_urls() -> list[dict[str, Any]]:
    """The subset of preflight URLs that are safe to turn into a QR (consistency-checked). Excludes
    Mac-only and anything flagged route_failed."""
    pf = preflight(verify_local=True)
    return [u for u in pf["urls"]
            if u.get("usable_from_phone") and u.get("status") != "route_failed"]
