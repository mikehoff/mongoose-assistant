"""Per-request connectivity context for browser/phone sessions.

Global status can say "LAN/DDNS is configured". This says what THIS request looks like: loopback,
private LAN/VPN, public/DDNS/proxied, or suspiciously mixed. It is read-only and intended for the UI to
explain why the same session works at home, fails through a city DDNS URL, or opens localhost on a phone.
"""
from __future__ import annotations

import ipaddress
from typing import Any


def _split_host_port(host: str) -> tuple[str, str]:
    h = (host or "").strip()
    if not h:
        return "", ""
    if h.startswith("[") and "]" in h:  # IPv6 literal
        end = h.find("]")
        return h[1:end], h[end + 1:].lstrip(":")
    if h.count(":") == 1:
        a, b = h.rsplit(":", 1)
        if b.isdigit():
            return a, b
    return h, ""


def classify_client_host(host: str) -> dict[str, Any]:
    raw = (host or "").strip()
    h, _port = _split_host_port(raw)
    if not h:
        return {"host": raw, "kind": "unknown", "scope": "unknown", "is_loopback": False, "is_private": False}
    if h.lower() == "localhost":
        return {"host": raw, "kind": "hostname", "scope": "loopback", "is_loopback": True, "is_private": True}
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return {"host": raw, "kind": "hostname", "scope": "public_or_ddns", "is_loopback": False, "is_private": False}
    if ip.is_loopback:
        scope = "loopback"
    elif ip.is_private:
        scope = "private_lan_or_vpn"
    elif ip.is_link_local:
        scope = "link_local"
    else:
        scope = "public_or_proxied"
    return {"host": raw, "kind": "ip", "scope": scope, "is_loopback": ip.is_loopback, "is_private": ip.is_private}


def describe_request_context(request) -> dict[str, Any]:
    headers = getattr(request, "headers", {})
    host_header = headers.get("host", "") if headers else ""
    fwd_for = headers.get("x-forwarded-for", "") if headers else ""
    fwd_host = headers.get("x-forwarded-host", "") if headers else ""
    fwd_proto = headers.get("x-forwarded-proto", "") if headers else ""
    client = getattr(getattr(request, "client", None), "host", "") or ""
    visible_client = (fwd_for.split(",")[0].strip() if fwd_for else client)
    requested_host = fwd_host or host_header
    scheme = fwd_proto or getattr(getattr(request, "url", None), "scheme", "") or "http"
    client_info = classify_client_host(visible_client)
    host_name, _host_port = _split_host_port(requested_host)
    host_info = classify_client_host(host_name)
    warnings: list[str] = []
    if host_info["scope"] == "loopback" and not client_info["is_loopback"]:
        warnings.append("This request is using a loopback host from a non-loopback client. On a phone, localhost/127.0.0.1 points at the phone, not the Mac running Mongoose.")
    if scheme == "http":
        try:
            from ..access_urls import default_scheme
            if default_scheme() == "https":
                warnings.append("The request is HTTP, but this Mongoose install normally speaks HTTPS. Mixed HTTP/HTTPS is a common cause of readiness probes or browser flows failing.")
        except Exception:
            pass
    if client_info["scope"] == "private_lan_or_vpn" and host_info["scope"] == "public_or_ddns":
        warnings.append("A private-LAN client is reaching a public/DDNS hostname. That depends on router hairpin/NAT-loopback behaviour and may differ at home vs away.")
    if client_info["scope"] == "public_or_proxied" and host_info["scope"] in {"loopback", "private_lan_or_vpn"}:
        warnings.append("A public/proxied client appears to be targeting a local-only host. Use the configured public/DDNS URL for away-from-home access.")
    try:
        from ..access_urls import configured_public_base, lan_browser_base, local_browser_base
        candidates = {
            "local_mac": local_browser_base(),
            "lan_phone": lan_browser_base(),
            "public_ddns": configured_public_base(),
        }
    except Exception as exc:  # noqa: BLE001
        candidates = {"error": f"url candidates unavailable: {type(exc).__name__}: {exc}"[:180]}
    return {
        "ok": True,
        "client": client_info,
        "host": host_info,
        "headers": {
            "host": host_header,
            "x_forwarded_for_present": bool(fwd_for),
            "x_forwarded_host": fwd_host,
            "x_forwarded_proto": fwd_proto,
        },
        "requested_base": f"{scheme}://{requested_host}" if requested_host else "",
        "access_channel": client_info["scope"],
        "warnings": warnings,
        "candidate_urls": candidates,
        "guidance": "Use local_mac on the Mac, lan_phone on the same home network, and public_ddns when genuinely away from home. Do not reuse a localhost session on a phone.",
    }
