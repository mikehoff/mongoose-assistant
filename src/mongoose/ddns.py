"""Dynamic DNS / remote-reach so Mongoose stays reachable by a stable name when your home IP changes.

There are two genuinely different categories here, and conflating them would be dishonest:

  DYNAMIC DNS (duckdns, dynu, noip, cloudflare): you own a hostname; your home IP changes; a small
  loop periodically tells the provider the new IP. IMPORTANT TRADE-OFFS, stated plainly:
    - this exposes your home's public IP in DNS, and
    - it does NOT by itself let anything reach Mongoose, you also need a port-forward on your
      router (and enable-phone, so Mongoose listens on the LAN). DDNS only keeps the NAME pointing
      at the right IP.

  TAILSCALE: a private mesh VPN. No port-forward, no public IP exposure, no open ports. Your devices
  join a private network and reach Mongoose at a Tailscale name (e.g. mac-mini.tailXXXX.ts.net).
  For a privacy-first local assistant this is the safer option, so we recommend it as the default,
  but we let you choose.

The "automatic script that updates the IP" is the shared engine for the DDNS providers: detect the
current public IP, and if it changed, call that provider's update endpoint. It runs on the scheduler
(every `ddns_interval_minutes`) and on demand via `mongoose ddns update`.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

# --- provider catalogue ----------------------------------------------------------------------

@dataclass
class DDNSProvider:
    slug: str
    name: str
    kind: str                 # "ddns" or "vpn"
    needs_portforward: bool
    blurb: str
    # for ddns providers: which env keys configure it, and an updater(env, ip) -> (ok, detail)
    config_keys: tuple[str, ...] = ()
    setup_hint: str = ""


def _update_duckdns(env: dict, ip: str) -> tuple[bool, str]:
    domain = env.get("DDNS_DUCKDNS_DOMAIN", "").strip()
    token = env.get("DDNS_DUCKDNS_TOKEN", "").strip()
    if not (domain and token):
        return False, "DuckDNS domain/token not set"
    import urllib.request
    # DuckDNS: domains= without the .duckdns.org suffix
    sub = domain.replace(".duckdns.org", "")
    url = f"https://www.duckdns.org/update?domains={sub}&token={token}&ip={ip}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            body = r.read().decode().strip()
        return (body == "OK", f"DuckDNS responded: {body}")
    except Exception as exc:  # noqa: BLE001
        return False, f"DuckDNS update failed: {exc}"


def _update_dynu(env: dict, ip: str) -> tuple[bool, str]:
    host = env.get("DDNS_DYNU_HOSTNAME", "").strip()
    user = env.get("DDNS_DYNU_USERNAME", "").strip()
    pw = env.get("DDNS_DYNU_PASSWORD", "").strip()  # Dynu IP-update password (not the account pw)
    if not (host and user and pw):
        return False, "Dynu hostname/username/password not set"
    import urllib.request
    import base64
    url = f"https://api.dynu.com/nic/update?hostname={host}&myip={ip}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode().strip()
        ok = body.startswith(("good", "nochg"))
        return ok, f"Dynu responded: {body}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Dynu update failed: {exc}"


def _update_noip(env: dict, ip: str) -> tuple[bool, str]:
    host = env.get("DDNS_NOIP_HOSTNAME", "").strip()
    user = env.get("DDNS_NOIP_USERNAME", "").strip()
    pw = env.get("DDNS_NOIP_PASSWORD", "").strip()
    if not (host and user and pw):
        return False, "No-IP hostname/username/password not set"
    import urllib.request
    import base64
    url = f"https://dynupdate.no-ip.com/nic/update?hostname={host}&myip={ip}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    req.add_header("User-Agent", "Mongoose-DDNS/1.0 mongoose@localhost")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode().strip()
        ok = body.startswith(("good", "nochg"))
        return ok, f"No-IP responded: {body}"
    except Exception as exc:  # noqa: BLE001
        return False, f"No-IP update failed: {exc}"


def _update_cloudflare(env: dict, ip: str) -> tuple[bool, str]:
    token = env.get("DDNS_CLOUDFLARE_TOKEN", "").strip()
    zone = env.get("DDNS_CLOUDFLARE_ZONE_ID", "").strip()
    record = env.get("DDNS_CLOUDFLARE_RECORD_NAME", "").strip()
    if not (token and zone and record):
        return False, "Cloudflare token/zone/record not set"
    import urllib.request
    import json as _json
    base = "https://api.cloudflare.com/client/v4"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        # find the A record id for this name
        q = urllib.request.Request(
            f"{base}/zones/{zone}/dns_records?type=A&name={record}", headers=headers)
        with urllib.request.urlopen(q, timeout=15) as r:
            data = _json.loads(r.read().decode())
        results = data.get("result", [])
        if not results:
            return False, f"Cloudflare: no A record named {record} in that zone"
        rec_id = results[0]["id"]
        if results[0].get("content") == ip:
            return True, "Cloudflare: already up to date"
        payload = _json.dumps({"type": "A", "name": record, "content": ip,
                               "ttl": 120, "proxied": results[0].get("proxied", False)}).encode()
        upd = urllib.request.Request(f"{base}/zones/{zone}/dns_records/{rec_id}",
                                     data=payload, headers=headers, method="PUT")
        with urllib.request.urlopen(upd, timeout=15) as r:
            ud = _json.loads(r.read().decode())
        return (bool(ud.get("success")), "Cloudflare updated" if ud.get("success") else "Cloudflare update rejected")
    except Exception as exc:  # noqa: BLE001
        return False, f"Cloudflare update failed: {exc}"


_UPDATERS: dict[str, Callable[[dict, str], tuple[bool, str]]] = {
    "duckdns": _update_duckdns,
    "dynu": _update_dynu,
    "noip": _update_noip,
    "cloudflare": _update_cloudflare,
}

PROVIDERS: list[DDNSProvider] = [
    DDNSProvider(
        "tailscale", "Tailscale (private VPN, recommended)", "vpn", needs_portforward=False,
        blurb="A private mesh VPN. No port-forwarding, no open ports, your home IP is never exposed. "
              "Your devices reach Mongoose over an encrypted private network. Best fit for a "
              "privacy-first assistant.",
        setup_hint="Installs the Tailscale app and runs `tailscale up`. You sign in once; then your "
                   "phone/laptop (also on Tailscale) reach the Mac by its tailnet name."),
    DDNSProvider(
        "duckdns", "DuckDNS (free dynamic DNS)", "ddns", needs_portforward=True,
        blurb="Free. Gives you a name like yourname.duckdns.org that this keeps pointed at your home "
              "IP. Needs a router port-forward to actually reach Mongoose, and exposes your public IP.",
        config_keys=("DDNS_DUCKDNS_DOMAIN", "DDNS_DUCKDNS_TOKEN"),
        setup_hint="At duckdns.org (sign in), create a subdomain and copy your token."),
    DDNSProvider(
        "dynu", "Dynu (free dynamic DNS)", "ddns", needs_portforward=True,
        blurb="Free tier. A hostname kept pointed at your home IP. Needs a router port-forward and "
              "exposes your public IP.",
        config_keys=("DDNS_DYNU_HOSTNAME", "DDNS_DYNU_USERNAME", "DDNS_DYNU_PASSWORD"),
        setup_hint="At dynu.com, create a DDNS hostname; use your username and the IP-update password."),
    DDNSProvider(
        "noip", "No-IP (free dynamic DNS)", "ddns", needs_portforward=True,
        blurb="Free tier (requires periodic confirmation). A hostname kept pointed at your home IP. "
              "Needs a router port-forward and exposes your public IP.",
        config_keys=("DDNS_NOIP_HOSTNAME", "DDNS_NOIP_USERNAME", "DDNS_NOIP_PASSWORD"),
        setup_hint="At noip.com, create a hostname; use your account username and password."),
    DDNSProvider(
        "cloudflare", "Cloudflare (your own domain)", "ddns", needs_portforward=True,
        blurb="If you own a domain on Cloudflare, keeps an A record pointed at your home IP. Needs a "
              "router port-forward and exposes your public IP (unless you also set up a tunnel).",
        config_keys=("DDNS_CLOUDFLARE_TOKEN", "DDNS_CLOUDFLARE_ZONE_ID", "DDNS_CLOUDFLARE_RECORD_NAME"),
        setup_hint="Create a scoped API token (Zone:DNS:Edit), and copy the Zone ID and the A-record "
                   "name (e.g. home.yourdomain.com) from the Cloudflare dashboard."),
]


def provider_by_slug(slug: str) -> DDNSProvider | None:
    return next((p for p in PROVIDERS if p.slug == slug), None)


# human labels + secret flags for each provider key, so the browser can render proper fields
_FIELD_META: dict[str, dict] = {
    "DDNS_DUCKDNS_DOMAIN": {"label": "DuckDNS subdomain", "secret": False,
                            "hint": "Just the name you created, e.g. 'myhome' (the .duckdns.org is added for you)."},
    "DDNS_DUCKDNS_TOKEN": {"label": "DuckDNS token", "secret": True,
                           "hint": "The token shown on your duckdns.org account page."},
    "DDNS_DYNU_HOSTNAME": {"label": "Dynu hostname", "secret": False,
                           "hint": "The full hostname you created, e.g. myhome.freeddns.org."},
    "DDNS_DYNU_USERNAME": {"label": "Dynu username", "secret": False, "hint": "Your Dynu account username."},
    "DDNS_DYNU_PASSWORD": {"label": "Dynu IP-update password", "secret": True,
                           "hint": "The IP Update Password from Dynu (under Control Panel), not your login password."},
    "DDNS_NOIP_HOSTNAME": {"label": "No-IP hostname", "secret": False,
                           "hint": "The hostname you created at no-ip.com, e.g. myhome.ddns.net."},
    "DDNS_NOIP_USERNAME": {"label": "No-IP username", "secret": False, "hint": "Your No-IP account username/email."},
    "DDNS_NOIP_PASSWORD": {"label": "No-IP password", "secret": True, "hint": "Your No-IP account password."},
    "DDNS_CLOUDFLARE_TOKEN": {"label": "Cloudflare API token", "secret": True,
                              "hint": "A scoped token with Zone:DNS:Edit permission (My Profile, API Tokens)."},
    "DDNS_CLOUDFLARE_ZONE_ID": {"label": "Cloudflare Zone ID", "secret": False,
                                "hint": "On your domain's Overview page in Cloudflare, lower right."},
    "DDNS_CLOUDFLARE_RECORD_NAME": {"label": "DNS record name", "secret": False,
                                    "hint": "The A record to keep updated, e.g. home.yourdomain.com."},
}


def provider_fields(slug: str) -> list[dict]:
    """Field descriptors for the browser form: key, label, secret, hint."""
    prov = provider_by_slug(slug)
    if not prov:
        return []
    return [{"key": k, **_FIELD_META.get(k, {"label": k, "secret": "TOKEN" in k or "PASSWORD" in k, "hint": ""})}
            for k in prov.config_keys]


def options_payload(env: dict | None = None) -> dict:
    """Everything the browser needs to render the DDNS chooser: the providers (with category, blurb,
    port-forward/IP-exposure facts, and fields), plus current state and which values are already set."""
    if env is None:
        from .setup_wizard import effective_env
        env = effective_env()
    opts = []
    for p in PROVIDERS:
        fields = []
        for f in provider_fields(p.slug):
            fields.append({**f, "is_set": bool(env.get(f["key"]))})
        opts.append({
            "slug": p.slug, "name": p.name, "kind": p.kind,
            "recommended": p.slug == "tailscale",
            "needs_portforward": p.needs_portforward,
            "exposes_public_ip": p.kind == "ddns",
            "blurb": p.blurb, "setup_hint": p.setup_hint,
            "fields": fields,
        })
    return {"providers": opts, "status": status(env)}


def save_ddns(provider: str, values: dict, do_update: bool = True) -> dict:
    """Persist a chosen DDNS provider + its field values (the browser path, no prompts). For DDNS
    providers, validates required fields are present and does an immediate update for feedback. For
    Tailscale this only records the choice (the install/up step is a separate explicit action, since
    it needs brew/sudo and can't run from a web request)."""
    from .setup_wizard import write_env
    prov = provider_by_slug(provider)
    if not prov:
        return {"ok": False, "detail": f"Unknown provider '{provider}'."}

    if prov.kind == "vpn":
        write_env({"DDNS_PROVIDER": "tailscale", "DDNS_ENABLED": "true"})
        return {"ok": True, "detail": "Tailscale selected. Finish by installing/bringing it up (button below or `mongoose ddns setup`).",
                "provider": "tailscale", "needs_install": True}

    missing = [k for k in prov.config_keys if not (values.get(k) or "").strip()]
    if missing:
        nice = ", ".join(_FIELD_META.get(k, {}).get("label", k) for k in missing)
        return {"ok": False, "detail": f"Please fill in: {nice}."}

    updates = {"DDNS_PROVIDER": prov.slug, "DDNS_ENABLED": "true"}
    for k in prov.config_keys:
        updates[k] = values[k].strip()
    write_env(updates)

    if not do_update:
        return {"ok": True, "detail": f"Saved {prov.name}.", "provider": prov.slug}

    res = update_now(force=True)
    return {"ok": bool(res.get("ok")), "saved": True, "provider": prov.slug,
            "detail": (f"Saved. {res.get('detail','')}" if res.get("ok")
                       else f"Saved, but the first update didn't succeed: {res.get('detail','')}"),
            "ip": res.get("ip")}


def disable_ddns() -> dict:
    """Turn auto-update off without wiping the entered credentials."""
    from .setup_wizard import write_env
    write_env({"DDNS_ENABLED": "false"})
    return {"ok": True, "detail": "Auto-update disabled. Your settings are kept; re-enable any time."}


# --- the auto-update engine ------------------------------------------------------------------

def detect_public_ip(timeout: float = 10.0) -> str | None:
    """Best-effort current public IPv4. Tries a couple of plain-text IP echoes."""
    import urllib.request
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                ip = r.read().decode().strip()
            # basic sanity: dotted quad
            parts = ip.split(".")
            if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                return ip
        except Exception:
            continue
    return None


def _last_ip_path():
    from .settings import get_settings
    from pathlib import Path
    return Path(getattr(get_settings(), "data_dir", "data")) / "ddns_last_ip"


def update_now(env: dict | None = None, force: bool = False) -> dict:
    """Detect the public IP and, if it changed (or force), update the configured DDNS provider.
    Returns a result dict. For Tailscale this is a no-op (it has its own daemon)."""
    if env is None:
        from .setup_wizard import effective_env
        env = effective_env()
    provider = (env.get("DDNS_PROVIDER", "") or "").strip()
    if not provider:
        return {"ok": False, "detail": "No DDNS provider chosen. Run: mongoose ddns setup"}
    if provider == "tailscale":
        return {"ok": True, "detail": "Tailscale manages its own connection; no IP update needed.",
                "provider": "tailscale", "noop": True}
    updater = _UPDATERS.get(provider)
    if not updater:
        return {"ok": False, "detail": f"Unknown DDNS provider: {provider}"}

    ip = detect_public_ip()
    if not ip:
        return {"ok": False, "detail": "Could not detect the current public IP."}

    last = None
    p = _last_ip_path()
    try:
        if p.exists():
            last = p.read_text().strip()
    except Exception:
        last = None

    if last == ip and not force:
        return {"ok": True, "detail": f"IP unchanged ({ip}); no update needed.", "ip": ip,
                "provider": provider, "changed": False}

    ok, detail = updater(env, ip)
    if ok:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(ip)
        except Exception:
            pass
    return {"ok": ok, "detail": detail, "ip": ip, "provider": provider, "changed": True}


def status(env: dict | None = None) -> dict:
    """Current DDNS configuration/state, for the cockpit and CLI."""
    if env is None:
        from .setup_wizard import effective_env
        env = effective_env()
    provider = (env.get("DDNS_PROVIDER", "") or "").strip()
    prov = provider_by_slug(provider) if provider else None
    last = None
    try:
        p = _last_ip_path()
        if p.exists():
            last = p.read_text().strip()
    except Exception:
        last = None
    configured = False
    if prov and prov.kind == "ddns":
        configured = all(env.get(k) for k in prov.config_keys)
    elif prov and prov.kind == "vpn":
        configured = shutil.which("tailscale") is not None
    return {
        "provider": provider,
        "provider_name": prov.name if prov else "",
        "kind": prov.kind if prov else "",
        "configured": configured,
        "needs_portforward": prov.needs_portforward if prov else False,
        "last_ip": last,
        "enabled": str(env.get("DDNS_ENABLED", "")).lower() in ("1", "true", "yes"),
    }


# --- Tailscale (the VPN path) ----------------------------------------------------------------

def setup_tailscale(yes: bool = False) -> int:
    """Install Tailscale and bring it up. macOS via Homebrew; Linux points to the install docs."""
    import platform
    if platform.system() == "Darwin":
        if not shutil.which("tailscale"):
            if not shutil.which("brew"):
                print("Homebrew isn't installed; it's the easy way to get Tailscale.")
                print("Install from https://brew.sh, then re-run: mongoose ddns setup")
                return 1
            if not yes:
                ans = input("Install Tailscale via Homebrew now? [Y/n] ").strip().lower()
                if ans in ("n", "no"):
                    return 1
            print("Installing Tailscale (brew install tailscale)...")
            subprocess.run(["brew", "install", "tailscale"])
        # start the daemon + bring up (this opens a browser sign-in the first time)
        print("Bringing Tailscale up, a browser may open for you to sign in.")
        subprocess.run(["sudo", "tailscale", "up"])
        # record the choice
        from .setup_wizard import write_env
        write_env({"DDNS_PROVIDER": "tailscale", "DDNS_ENABLED": "true"})
        try:
            out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
            ts_ip = (out.stdout or "").strip().splitlines()[0] if out.stdout.strip() else ""
        except Exception:
            ts_ip = ""
        print("\nTailscale is set up. On your phone/laptop, install Tailscale and sign in to the same")
        print("account; then reach Mongoose at the Mac's tailnet name (or its 100.x address" +
              (f", e.g. http://{ts_ip}:8765" if ts_ip else "") + ").")
        print("No router port-forwarding needed, and your home IP is never exposed.")
        return 0
    print("On this OS, install Tailscale from https://tailscale.com/download, then `tailscale up`.")
    print("After that, Mongoose is reachable at the Mac's tailnet name. No port-forwarding needed.")
    return 1


# ── Public URL (for webhook callbacks) ──────────────────────────────────────

def upgrade_stale_public_scheme(url: str, env: dict | None = None) -> str:
    """A stale, explicit MONGOOSE_PUBLIC_URL=http://... override never re-checks reality: once
    set, it advertises http:// forever, even after the owner later enables HTTPS, because
    "explicit value wins" (correctly, so a deliberate manual override isn't silently
    overwritten) also meant it never got RE-EVALUATED against the one fact that should
    change it. This is the single, shared fix: called from every place an explicit public
    URL is read, so the upgrade happens once, consistently, rather than each caller needing
    to remember to re-check tls_is_enabled itself.

    Upgrades http:// -> https:// only when Mongoose is ACTUALLY serving HTTPS right now
    (tls_is_enabled, the same real cert+key check every other scheme decision uses), and
    never when the owner has deliberately set MONGOOSE_TLS=off, which is an explicit signal
    they want plain HTTP regardless of what's technically available."""
    if not url or not url.lower().startswith("http://"):
        return url
    if env is None:
        try:
            from .setup_wizard import effective_env
            env = effective_env()
        except Exception:
            env = {}
    tls_off = str(env.get("MONGOOSE_TLS", "") or "").strip().lower() in ("off", "0", "false", "no", "none")
    if tls_off:
        return url
    if not tls_is_enabled(env):
        return url
    return "https://" + url[len("http://"):]


def public_url(env: dict | None = None) -> str | None:
    """Return the full externally-reachable Mongoose URL, or None if not configured.

    Used to tell example.org (or any external site) where to POST webhook callbacks, and to build
    pairing/QR URLs. Assembles from the configured DDNS hostname + MONGOOSE_PORT. The SCHEME matches what
    the server actually speaks, https only when TLS is genuinely enabled (cert+key present, via
    tls_is_enabled), otherwise http. Returns None when DDNS is not configured.

    Examples (https shown only when TLS is on; otherwise the same hosts with http://):
      DuckDNS:    https://mymac.duckdns.org:8765  (or http:// when HTTP-only)
      Dynu/noip:  https://mymac.dynu.net:8765
      Cloudflare: https://home.example.org:8765
      Tailscale:  not applicable (private VPN, not reachable from internet)
    """
    if env is None:
        try:
            from .setup_wizard import effective_env
            env = effective_env()
        except Exception:
            env = {}
    # The explicit canonical URL wins, exactly as in services.remote_access.public_base_url.
    # External audit of 1.50.4 found four DDNS-era callers (guest QR fallback, /pair/claim
    # handoff, /device/remote/proof, webhook URLs) reaching this function directly and
    # therefore claiming "no public route" while a proven Cloudflare tunnel was live, which
    # made the cockpit's "pairing and guest invites now use this address" copy false.
    # Honoring MONGOOSE_PUBLIC_URL here heals every such caller at once, present and future.
    import os as _os2
    _explicit = (env.get("MONGOOSE_PUBLIC_URL") or _os2.environ.get("MONGOOSE_PUBLIC_URL", "")).strip().rstrip("/")
    if _explicit:
        try:
            return upgrade_stale_public_scheme(_explicit, env)
        except Exception:
            return _explicit
    provider = (env.get("DDNS_PROVIDER") or "").strip()
    prov = provider_by_slug(provider) if provider else None
    if not prov or prov.kind != "ddns":
        return None  # Tailscale is private; no internet-facing URL
    # Extract the hostname from whichever provider-specific key carries it
    hostname = ""
    if provider == "duckdns":
        d = (env.get("DDNS_DUCKDNS_DOMAIN") or "").strip()
        if d:
            hostname = d if d.endswith(".duckdns.org") else f"{d}.duckdns.org"
    elif provider == "dynu":
        hostname = (env.get("DDNS_DYNU_HOSTNAME") or "").strip()
    elif provider == "noip":
        hostname = (env.get("DDNS_NOIP_HOSTNAME") or "").strip()
    elif provider == "cloudflare":
        hostname = (env.get("DDNS_CLOUDFLARE_RECORD_NAME") or "").strip()
    if not hostname:
        return None
    import os as _os
    # PUBLIC port vs INTERNAL port are genuinely different facts: the router forwards from
    # SOME external port to Mongoose's internal MONGOOSE_PORT, and those need not match (a
    # common, deliberate setup is external:443 -> internal:8765, since :443 avoids carriers
    # that block unusual outbound ports and looks like a normal website). Defaulting to
    # ASSUME they match is the safe choice (requires the LEAST router configuration: a
    # same-port forward), but an owner who has actually set up a different external mapping
    # needs a way to tell Mongoose the real public port, or every URL Mongoose advertises
    # points at a port nothing is forwarded to. MONGOOSE_PUBLIC_PORT is that explicit,
    # opt-in override; unset, behavior is unchanged from before this existed.
    public_port_override = (env.get("MONGOOSE_PUBLIC_PORT") or "").strip()
    port = public_port_override or _os.getenv("MONGOOSE_PORT", "8765").strip() or "8765"
    # CRITICAL: advertise the scheme the server ACTUALLY speaks, not a guess from the port. A DDNS URL
    # with https:// when the server is HTTP-only is the #1 pairing failure , the phone's connection
    # aborts before the app sees it (and the browser's HTTPS-first upgrade defeats a typed http:// too).
    # TLS is only really on when a cert + key are configured.
    https_on = tls_is_enabled(env)
    if https_on:
        scheme = "https"
    else:
        scheme = "http"
    # Port suppression: omit :443 for https and :80 for http (the defaults), keep everything else.
    if (https_on and port == "443") or (not https_on and port == "80"):
        port_part = ""
    else:
        port_part = f":{port}"
    return f"{scheme}://{hostname}{port_part}"


def public_listener_acceptance() -> dict[str, Any]:
    """Is Mongoose's OWN process even accepting connections right now, entirely independent
    of DNS or the router itself? This isolates a fact reachability_diagnosis cannot:
    self_reach_ok there connects THROUGH the router to the public IP, so a failure there
    could mean "router problem" OR "Mongoose problem" and can't fully tell them apart.

    FIX (caught by re-auditing this exact function): the original version checked ONLY
    127.0.0.1, then trusted CONFIG's own claim about host_setting to decide whether that
    implied the LAN-facing interface was ALSO accepting. That is exactly the class of bug
    already fixed elsewhere for the plain LAN-pairing path: a process started before
    MONGOOSE_HOST=0.0.0.0 was saved is still genuinely bound to loopback only, so it accepts
    127.0.0.1 happily while refusing the LAN IP a router's port-forward actually reaches. This
    now independently proves the LAN-facing interface too, the same real socket check
    _lan_socket_reachable already uses for the plain LAN path, so a stale, still-loopback-
    only process can never be misread as "listener_accepting" just because config says the
    right thing and loopback happens to work regardless."""
    out: dict[str, Any] = {"ok": False, "reason": "unknown", "host_setting": "", "allow_remote": False,
                           "token_present": False, "listener_accepting": False,
                           "loopback_accepting": None, "lan_accepting": None}
    try:
        from .settings import get_settings
        s = get_settings()
        out["host_setting"] = str(getattr(s, "host", "") or "")
        out["allow_remote"] = bool(getattr(s, "allow_remote", False))
        out["token_present"] = bool(getattr(s, "api_token", ""))
        port = int(getattr(s, "port", 8765) or 8765)
        import socket as _socket

        def _probe(host: str, timeout: float = 1.5) -> bool:
            try:
                with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
                    sock.settimeout(timeout)
                    return sock.connect_ex((host, port)) == 0
            except Exception:
                return False

        loopback_ok = _probe("127.0.0.1")
        out["loopback_accepting"] = loopback_ok
        if not loopback_ok:
            out["listener_accepting"] = False
            out["reason"] = "listener_not_accepting"
            return out

        lan_ip = ""
        try:
            from .discovery import _primary_ip
            lan_ip = _primary_ip() or ""
        except Exception:
            lan_ip = ""
        lan_ok = _probe(lan_ip) if lan_ip else False
        out["lan_accepting"] = lan_ok
        # Both must genuinely accept: loopback alone proves the process is running, but a
        # router's forwarded traffic reaches the LAN-facing address specifically, and that is
        # the one a stale loopback-only bind would fail while still passing the check above.
        out["listener_accepting"] = bool(loopback_ok and lan_ok)

        if not out["listener_accepting"]:
            out["reason"] = "listener_loopback_only"
        elif out["host_setting"] not in ("0.0.0.0", "::"):
            out["reason"] = "bound_loopback_only"
        elif not out["allow_remote"]:
            out["reason"] = "allow_remote_off"
        elif not out["token_present"]:
            out["reason"] = "no_token"
        else:
            out["ok"] = True
            out["reason"] = "accepting"
    except Exception as exc:
        out["reason"] = f"error: {exc}"[:200]
    return out


def _upnp_discover_igd(timeout: float = 3.0) -> dict[str, Any] | None:
    """SSDP multicast discovery for a UPnP Internet Gateway Device (the router), then fetch
    its device description to find the WAN connection service's controlURL. Stdlib only.

    HONEST LIMIT: this has been written carefully against the UPnP IGD/SSDP spec, but has
    not been exercised against a real consumer router in this environment (no such device
    is reachable here). Treat this as unverified against real hardware until it has actually
    run on a real home network."""
    import socket as _socket
    ssdp_request = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        "MAN: \"ssdp:discover\"\r\n"
        "MX: 2\r\n"
        "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n"
    ).encode("utf-8")
    try:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(ssdp_request, ("239.255.255.250", 1900))
        location = None
        try:
            while True:
                data, _addr = sock.recvfrom(4096)
                text = data.decode("utf-8", errors="ignore")
                for line in text.split("\r\n"):
                    if line.lower().startswith("location:"):
                        location = line.split(":", 1)[1].strip()
                        break
                if location:
                    break
        except _socket.timeout:
            pass
        finally:
            sock.close()
        if not location:
            return None
    except Exception:
        return None

    try:
        import urllib.request as _ur
        with _ur.urlopen(location, timeout=timeout) as resp:
            xml_text = resp.read().decode("utf-8", errors="ignore")
        import re as _re
        # Find a WAN connection service block and pull its serviceType + controlURL. A real
        # device description nests <service> blocks inside <serviceList>; a full XML parse
        # would be more robust than regex, but device descriptions vary enough in namespace
        # prefixing that a targeted regex is the pragmatic, still-correct-enough choice here.
        service_match = _re.search(
            r"<service>\s*<serviceType>(urn:schemas-upnp-org:service:WAN(?:IP|PPP)Connection:\d)"
            r"</serviceType>.*?<controlURL>([^<]+)</controlURL>", xml_text, _re.S)
        if not service_match:
            return None
        service_type = service_match.group(1)
        control_path = service_match.group(2)
        from urllib.parse import urljoin
        control_url = urljoin(location, control_path)
        return {"control_url": control_url, "service_type": service_type, "location": location}
    except Exception:
        return None


def _upnp_add_port_mapping(igd: dict[str, Any], external_port: int, internal_port: int,
                           internal_client: str, description: str = "Mongoose",
                           lease_seconds: int = 0, timeout: float = 4.0) -> dict[str, Any]:
    """Send the AddPortMapping SOAP request to a discovered IGD. lease_seconds=0 means
    permanent in the UPnP spec (some routers ignore this and always keep it until removed or
    reboot); Mongoose still records what it asked for."""
    soap_body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:AddPortMapping xmlns:u="{igd["service_type"]}">'
        "<NewRemoteHost></NewRemoteHost>"
        f"<NewExternalPort>{external_port}</NewExternalPort>"
        "<NewProtocol>TCP</NewProtocol>"
        f"<NewInternalPort>{internal_port}</NewInternalPort>"
        f"<NewInternalClient>{internal_client}</NewInternalClient>"
        "<NewEnabled>1</NewEnabled>"
        f"<NewPortMappingDescription>{description}</NewPortMappingDescription>"
        f"<NewLeaseDuration>{lease_seconds}</NewLeaseDuration>"
        "</u:AddPortMapping>"
        "</s:Body></s:Envelope>"
    ).encode("utf-8")
    try:
        import urllib.request as _ur
        req = _ur.Request(igd["control_url"], data=soap_body, method="POST", headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{igd["service_type"]}#AddPortMapping"',
        })
        with _ur.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return {"ok": True, "external_port": external_port, "internal_port": internal_port}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


def attempt_upnp_repair(internal_port: int, external_port: int | None = None,
                        internal_client: str | None = None) -> dict[str, Any]:
    """Best-effort, single-port UPnP mapping attempt: internal_port on THIS Mac, mapped from
    external_port (defaults to the same port) on the router. Never touches more than the one
    port Mongoose actually uses. Returns method + result either way, never raises."""
    external_port = external_port or internal_port
    try:
        if internal_client is None:
            from .discovery import _primary_ip
            internal_client = _primary_ip() or ""
        if not internal_client:
            return {"ok": False, "method": "upnp", "error": "could not determine this Mac's LAN IP"}
        igd = _upnp_discover_igd()
        if not igd:
            return {"ok": False, "method": "upnp", "error": "no UPnP Internet Gateway Device found"}
        result = _upnp_add_port_mapping(igd, external_port, internal_port, internal_client,
                                        description="Mongoose remote access")
        result["method"] = "upnp"
        return result
    except Exception as exc:
        return {"ok": False, "method": "upnp", "error": str(exc)[:300]}


def reachability_diagnosis(env: dict | None = None, timeout: float = 3.0) -> dict[str, Any]:
    """Why a DDNS address might refuse a connection, distinguished into the things that
    actually cause it, since "ERR_CONNECTION_REFUSED" alone tells the owner nothing they can
    act on.

    FIX: this used to fold "Mongoose's own listener isn't accepting" into the same
    hairpin_or_no_portforward bucket as genuine router-side causes, which is a real
    diagnostic gap: a self-connect to the PUBLIC hostname:port fails identically whether the
    router can't loop back to itself, the port-forward doesn't exist, OR Mongoose's own
    process simply isn't listening (e.g. started before MONGOOSE_HOST/ALLOW_REMOTE took
    effect). Those need different fixes and pointing the owner at their router when the real
    cause is Mongoose's own stale process sends them on the wrong errand. The loopback check
    below rules Mongoose's own side in or out FIRST, before ever blaming the router.

    Three independent checks, all self-contained (no third-party port-checker dependency,
    which is fragile and adds an external trust requirement for a security-relevant probe):

    1. Listener acceptance: is Mongoose's own process actually listening at all, checked via
       loopback, with no router/DNS involved. If this fails, nothing else matters yet.

    2. DNS staleness: does the DDNS hostname currently resolve to THIS network's actual
       public IP? If not, the DDNS updater has fallen behind and no client anywhere, home or
       away, will reach the right place until it catches up.

    3. Hairpin / missing port-forward: with the listener confirmed accepting AND DNS
       confirmed correct, can THIS Mac reach its own public hostname:port by connecting out
       through its own router? If both of those are right but this still fails, the cause is
       genuinely router-side: NAT loopback/hairpin isn't supported (common on consumer
       routers, and only matters for clients on the SAME LAN, like a phone that just paired
       at home), or the port-forward to this Mac doesn't exist at all (which would ALSO block
       genuinely external clients, not just same-LAN ones). A self-test cannot fully
       distinguish these two router-side causes from the inside; both get named as
       possibilities rather than guessing which one it is.

    Honest limitation, stated rather than glossed over: none of these three checks proves a
    genuinely EXTERNAL client can reach this Mac. Only a real external vantage point can
    prove that (see /device/remote/proof, which records exactly that when a paired device
    reports reaching the real public URL from outside).
    """
    out: dict[str, Any] = {"ok": False, "reason": "not_configured", "hostname": "",
                           "dns_ip": None, "actual_public_ip": None, "dns_matches": None,
                           "self_reach_ok": None, "listener_accepting": None}
    try:
        url = public_url(env)
        if not url:
            return out
        import re as _re
        m = _re.match(r"https?://([^:/]+)(?::(\d+))?", url)
        if not m:
            out["reason"] = "unparseable_url"
            return out
        hostname = m.group(1)
        port = int(m.group(2) or (443 if url.startswith("https") else 80))
        out["hostname"] = hostname

        listener = public_listener_acceptance()
        listener_accepting = bool(listener.get("listener_accepting"))
        out["listener_accepting"] = listener_accepting
        if not listener_accepting:
            out["reason"] = "listener_not_accepting"
            return out

        import socket as _socket
        try:
            dns_ip = _socket.gethostbyname(hostname)
        except Exception:
            out["reason"] = "dns_does_not_resolve"
            return out
        out["dns_ip"] = dns_ip

        actual_ip = detect_public_ip(timeout=timeout)
        out["actual_public_ip"] = actual_ip
        if actual_ip and dns_ip != actual_ip:
            out["dns_matches"] = False
            out["reason"] = "dns_stale"
            return out
        out["dns_matches"] = True if actual_ip else None

        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                result = sock.connect_ex((dns_ip, port))
            out["self_reach_ok"] = (result == 0)
        except Exception:
            out["self_reach_ok"] = False

        if out["self_reach_ok"]:
            out["ok"] = True
            out["reason"] = "reachable"
        else:
            out["reason"] = "hairpin_or_no_portforward"
        return out
    except Exception as exc:
        out["reason"] = "probe_error"
        out["error"] = str(exc)[:200]
        return out


def tls_is_enabled(env: dict | None = None) -> bool:
    """True only if Mongoose is actually serving HTTPS, i.e. a TLS cert AND key are configured. This is
    the single source of truth for which scheme pairing URLs/QRs may advertise. Reading the env (not the
    port) is what stops us telling a phone to use https:// when the server only speaks http."""
    if env is None:
        try:
            from .setup_wizard import effective_env
            env = effective_env()
        except Exception:
            env = {}
    certfile = (env.get("MONGOOSE_TLS_CERTFILE") or "").strip()
    keyfile = (env.get("MONGOOSE_TLS_KEYFILE") or "").strip()
    if not (certfile and keyfile):
        # Fall back to live settings in case env wasn't passed and effective_env missed them.
        try:
            from .settings import get_settings
            s = get_settings()
            certfile = certfile or (getattr(s, "tls_certfile", "") or "")
            keyfile = keyfile or (getattr(s, "tls_keyfile", "") or "")
        except Exception:
            pass
    if not (certfile and keyfile):
        return False
    # If the paths are set but the files are gone, we are NOT serving https.
    try:
        from pathlib import Path as _P
        return _P(certfile).exists() and _P(keyfile).exists()
    except Exception:
        return bool(certfile and keyfile)


def _discover_lan_ip() -> str | None:
    """Best-effort discovery of this machine's LAN IP (the 'Server IP Address' the router needs). Opens
    a UDP socket toward a public address, no packets are actually sent, and reads the local end the OS
    would route through. Falls back to None if it can't tell."""
    import socket
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass
    return None


def _server_port() -> int:
    """The port Mongoose is actually serving on (from the pidfile if running, else the configured/default)."""
    try:
        from .services.server_watchdog import read_pidfile
        _, port = read_pidfile()
        if port:
            return int(port)
    except Exception:
        pass
    try:
        from .settings import get_settings
        p = getattr(get_settings(), "port", None)
        if p:
            return int(p)
    except Exception:
        pass
    return 8765


def port_forward_card(env: dict | None = None) -> dict:
    """Mongoose's own, friendly version of 'set this up on your router', so the user hears it from
    Mongoose, warmly and with the numbers already filled in, instead of from a bare table somewhere.

    Returns:
      needed: bool, whether a port-forward is actually required (DDNS providers need it; Tailscale/VPN
              does NOT, and we say so happily).
      fields: the exact router fields (service name, server IP, ports, protocol) ready to copy.
      speech / lines: a warm, plain-language explanation in Mongoose's voice.
    Defensive: never raises; if it can't find the LAN IP it still gives the shape with a gentle note."""
    try:
        st = status(env)
    except Exception:
        # Even if status can't be read, return a safe, honest shape rather than erroring the caller.
        return {"needed": False, "provider": "", "provider_name": "", "kind": "",
                "fields": None, "lan_ip": _discover_lan_ip(), "port": _server_port(),
                "speech": ("I couldn't read your remote-access settings just now, so there's nothing to "
                           "change on your router from me at the moment. Local access works as usual."),
                "lines": ["Couldn't read remote-access status; no router change suggested right now."]}
    provider = st.get("provider") or ""
    provider_name = st.get("provider_name") or provider or "your remote-access provider"
    kind = st.get("kind") or ""
    needs = bool(st.get("needs_portforward"))
    port = _server_port()
    lan_ip = _discover_lan_ip()

    if provider and kind == "vpn":
        # Tailscale and friends: the good news case , nothing to do on the router.
        return {
            "needed": False, "provider": provider, "provider_name": provider_name, "kind": kind,
            "fields": None, "lan_ip": lan_ip, "port": port,
            "speech": (f"Good news, you're using {provider_name}, so there's nothing to change on your "
                       f"router. Your phone reaches me over the private link, and your home address "
                       f"stays hidden. You're all set."),
            "lines": [f"{provider_name} doesn't need a port-forward.",
                      "Nothing to set up on the router, your home IP is never exposed."],
        }

    if not needs:
        # No remote provider configured, or one that doesn't need forwarding.
        return {
            "needed": False, "provider": provider, "provider_name": provider_name, "kind": kind,
            "fields": None, "lan_ip": lan_ip, "port": port,
            "speech": ("Nothing to set up on your router right now, I'm reachable on your home network "
                       "as is. If you later turn on remote access with dynamic DNS, I'll walk you "
                       "through the one router change you'd need."),
            "lines": ["No router change needed for local use."],
        }

    # DDNS path , a port-forward IS needed. Build the filled-in card.
    fields = {
        "service_name": "mongoose",
        "server_ip_address": lan_ip or "<this Mac's LAN IP>",
        "start_port": port, "end_port": port,
        "translation_start_port": port, "translation_end_port": port,
        "protocol": "TCP",
        "status": "Enable",
    }
    ip_phrase = (f"this Mac ({lan_ip})" if lan_ip
                 else "this Mac (I couldn't read its LAN IP automatically, find it in System Settings, "
                      "Network, or your router's device list)")
    return {
        "needed": True, "provider": provider, "provider_name": provider_name, "kind": kind,
        "fields": fields, "lan_ip": lan_ip, "port": port,
        "speech": (f"One quick thing to finish remote access with {provider_name}: your router needs a "
                   f"port-forward so the outside world can reach me. Forward TCP port {port} to "
                   f"{ip_phrase}. Name it 'mongoose' so you recognise it later. That's the only router "
                   f"change, once it's in, you'll be able to reach me from anywhere."),
        "lines": [
            f"Forward TCP port {port} to {lan_ip or 'this Mac on your LAN'}.",
            "Name the rule 'mongoose'.",
            f"External port {port} -> internal {lan_ip or '<LAN IP>'}:{port} (TCP).",
            "Heads up: this exposes your home IP, which is normal for dynamic DNS. "
            "If you'd rather not, Tailscale gives remote access with no port-forward and no exposure.",
        ],
    }
