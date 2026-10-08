"""Cloudflare Tunnel support: the guest-friendly public route for a Mongoose behind CGNAT.

Why this exists: DDNS + a router port-forward requires an inbound path to the Mac, and
carrier-grade NAT (e.g. many ISPs') structurally forbids inbound connections; no router or
Mac setting can fix that. A Cloudflare Tunnel dials OUT from the Mac to Cloudflare's edge,
so CGNAT never matters, and guests get a normal https://mongoose.example.com link with a
real trusted certificate on any unmodified phone.

One property worth naming because it removes a long-standing honest limitation: with DDNS,
a self-test from the Mac could never prove a genuinely external client could reach it
(hairpin masks everything). With a tunnel, a self-test IS an external-path test: the request
leaves the Mac, reaches Cloudflare's edge over the public internet, and comes back down the
tunnel, exercising the exact path a guest's phone uses. So tunnel proof from this machine
is real proof, not a proxy for it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

# The two keys that define tunnel mode. MONGOOSE_PUBLIC_URL is set alongside them by the
# guided setup so every existing surface (pairing, launchpad, PWA origin, guest invites)
# picks the tunnel hostname up with zero changes to those code paths.
ENV_PROVIDER_KEY = "MONGOOSE_TUNNEL_PROVIDER"
ENV_HOSTNAME_KEY = "MONGOOSE_TUNNEL_HOSTNAME"


def _managed_bin_dir() -> Path:
    from .settings import get_settings
    d = get_settings().data_dir / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _download_url() -> str:
    """The official static build for this platform. cloudflared is a single Go binary, so
    'install' is just a download + chmod; no package manager, no sudo, no terminal."""
    import platform
    sysname = platform.system().lower()
    mach = platform.machine().lower()
    arch = "arm64" if mach in ("arm64", "aarch64") else "amd64"
    base = "https://github.com/cloudflare/cloudflared/releases/latest/download"
    if sysname == "darwin":
        return f"{base}/cloudflared-darwin-{arch}.tgz"
    return f"{base}/cloudflared-linux-{arch}"


def install_cloudflared(timeout: float = 120.0) -> dict[str, Any]:
    """Download the official cloudflared binary into Mongoose's own data/bin, so the whole
    journey needs no Homebrew, no sudo, and no terminal. Safe to re-run; returns the
    existing binary if one is already usable."""
    existing = cloudflared_path()
    if existing:
        return {"ok": True, "path": existing, "already": True}
    url = _download_url()
    dest = _managed_bin_dir() / "cloudflared"
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
        if url.endswith(".tgz"):
            import tarfile
            import io
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
                member = next((m for m in tf.getmembers() if m.name.endswith("cloudflared")), None)
                if member is None:
                    return {"ok": False, "error": "archive did not contain a cloudflared binary"}
                extracted = tf.extractfile(member)
                dest.write_bytes(extracted.read())
        else:
            dest.write_bytes(raw)
        dest.chmod(0o755)
        return {"ok": True, "path": str(dest), "already": False}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


_LOGIN_PROC: dict[str, Any] = {}


def start_login() -> dict[str, Any]:
    """Spawn `cloudflared tunnel login` server-side and capture the approval URL it prints,
    so the UI can show it as a clickable button. cloudflared then waits; when the person
    approves in their browser, Cloudflare delivers cert.pem to ~/.cloudflared and login is
    complete -- poll tunnel_state()['logged_in'] to detect that. The only human step in the
    whole journey is that browser approval click, which is Cloudflare's own auth and cannot
    (and should not) be automated away."""
    if _logged_in():
        return {"ok": True, "already": True}
    binary = cloudflared_path()
    if not binary:
        return {"ok": False, "error": "cloudflared is not installed yet"}
    import re
    import threading
    import time as _time
    proc = _LOGIN_PROC.get("proc")
    if proc is not None and proc.poll() is None and _LOGIN_PROC.get("url"):
        return {"ok": True, "login_url": _LOGIN_PROC["url"], "already_waiting": True}
    p = subprocess.Popen([binary, "tunnel", "login"], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    _LOGIN_PROC["proc"] = p
    _LOGIN_PROC["url"] = ""

    def _reader() -> None:
        try:
            for line in p.stdout or []:
                m = re.search(r"(https://\S+)", line)
                if m and not _LOGIN_PROC["url"]:
                    _LOGIN_PROC["url"] = m.group(1)
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()
    deadline = _time.time() + 15
    while _time.time() < deadline and not _LOGIN_PROC["url"]:
        if p.poll() is not None and _logged_in():
            return {"ok": True, "already": True}
        _time.sleep(0.2)
    if not _LOGIN_PROC["url"]:
        return {"ok": False, "error": "cloudflared did not produce a login URL in time"}
    return {"ok": True, "login_url": _LOGIN_PROC["url"]}


def _run(binary: str, *args: str, timeout: float = 60.0) -> tuple[int, str]:
    try:
        r = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as exc:
        return 1, str(exc)


def _tunnel_pidfile() -> Path:
    from .settings import get_settings
    return get_settings().data_dir / "cloudflared.pid"


_CF_AGENT_LABEL = "com.mongoose.cloudflared"


def _cf_agent_plist_path():
    from pathlib import Path as _P
    return _P.home() / "Library" / "LaunchAgents" / (_CF_AGENT_LABEL + ".plist")


def _cf_agent_plist_xml(binary: str, cfg: str, logdir: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!-- Mongoose cloudflared boot agent. Runs the guest tunnel at login and keeps it alive.\n'
        '     cloudflared and its config live outside the TCC-protected Downloads folder, so this\n'
        '     survives reboots even though the Mongoose SERVER cannot auto-start from Downloads. -->\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        '  <key>Label</key><string>' + _CF_AGENT_LABEL + '</string>\n'
        '  <key>ProgramArguments</key><array>\n'
        '    <string>' + binary + '</string><string>--no-autoupdate</string>\n'
        '    <string>--config</string><string>' + cfg + '</string>\n'
        '    <string>tunnel</string><string>run</string>\n'
        '  </array>\n'
        '  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n'
        '  <key>StandardOutPath</key><string>' + logdir + '/cloudflared.out.log</string>\n'
        '  <key>StandardErrorPath</key><string>' + logdir + '/cloudflared.err.log</string>\n'
        '  <key>ProcessType</key><string>Background</string>\n'
        '</dict></plist>\n'
    )


def cloudflared_boot_agent_status() -> dict[str, Any]:
    """Is the cloudflared boot agent installed (plist present), loaded (launchctl knows it), and
    actually running a cloudflared? Each fact checked independently (plist-on-disk alone is a trap)."""
    plist = _cf_agent_plist_path()
    loaded = running = False
    try:
        r = subprocess.run(["launchctl", "list", _CF_AGENT_LABEL], capture_output=True, timeout=10)
        loaded = (r.returncode == 0)
    except Exception:
        pass
    try:
        r = subprocess.run(["pgrep", "-f", "cloudflared.*tunnel run"], capture_output=True, timeout=5)
        running = (r.returncode == 0 and bool((r.stdout or b"").strip()))
    except Exception:
        pass
    return {"installed": plist.exists(), "loaded": loaded, "running": running}


def ensure_cloudflared_boot_agent() -> dict[str, Any]:
    """Install and load the cloudflared boot LaunchAgent if missing, and REPAIR it if the plist
    drifted (wrong binary or config path). Idempotent and safe to call on every serve start. This
    is why the tunnel survives a reboot even though the server, in Downloads, cannot. No-op if
    cloudflared or the tunnel config is not ready yet."""
    import os as _os
    import time as _t
    binary = cloudflared_path()
    cfg = _managed_config_dir() / "config.yml"
    if not binary or not cfg.exists():
        return {"ok": False, "reason": "cloudflared or config not ready", **cloudflared_boot_agent_status()}
    status = cloudflared_boot_agent_status()
    # HEALTHY -> leave a working agent completely alone. An earlier version rewrote on cosmetic
    # plist differences and booted the live tunnel out; healing must only touch a BROKEN agent,
    # and only judge "broken" by function (loaded + running + correct binary/config paths), never
    # by exact plist text.
    if status["installed"] and status["loaded"] and status["running"]:
        try:
            body = _cf_agent_plist_path().read_text()
            if str(binary) in body and str(cfg) in body:
                return {"ok": True, "repaired": False, **status}
        except Exception:
            return {"ok": True, "repaired": False, **status}
    # Broken/missing/wrong-paths: (re)write the plist and (re)load it, with a settle gap so the
    # bootstrap does not race the bootout (that race is what left it unloaded before).
    from .settings import get_settings
    logdir = get_settings().data_dir / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    plist = _cf_agent_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(_cf_agent_plist_xml(str(binary), str(cfg), str(logdir)))
    uid = _os.getuid()
    try:
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{_CF_AGENT_LABEL}"], capture_output=True, timeout=10)
        _t.sleep(2)
    except Exception:
        pass
    for argv in (["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                 ["launchctl", "enable", f"gui/{uid}/{_CF_AGENT_LABEL}"]):
        try:
            subprocess.run(argv, capture_output=True, timeout=15)
        except Exception:
            pass
    _t.sleep(2)
    return {"ok": True, "repaired": True, **cloudflared_boot_agent_status()}


def ensure_tunnel_running() -> dict[str, Any]:
    """Keep the guest tunnel up. Preferred path: a boot LaunchAgent (com.mongoose.cloudflared)
    that survives reboots because cloudflared lives outside the TCC-protected Downloads folder;
    Mongoose self-heals that agent here on every serve start. Only if the agent path cannot run
    cloudflared do we fall back to Mongoose supervising a detached `cloudflared tunnel run`
    itself (no sudo, no system service)."""
    agent = ensure_cloudflared_boot_agent()
    if agent.get("running"):
        return {"ok": True, "via_boot_agent": True, "agent": agent}
    if _process_running():
        return {"ok": True, "already": True}
    binary = cloudflared_path()
    if not binary:
        return {"ok": False, "error": "cloudflared is not installed"}
    if not _config_file():
        return {"ok": False, "error": "no tunnel config yet"}
    try:
        from .settings import get_settings
        log_path = get_settings().data_dir / "logs" / "cloudflared.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as logf:
            p = subprocess.Popen([binary, "--config", str(_managed_config_dir() / "config.yml"),
                                  "tunnel", "run"], stdout=logf, stderr=logf,
                                 start_new_session=True)
        _tunnel_pidfile().write_text(str(p.pid))
        return {"ok": True, "pid": p.pid}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


def _lookup(rrtype: str, name: str, timeout: float = 5.0) -> list[str]:
    """NS/MX lookup via dig, then host, then dnspython, whichever exists; empty list means
    'could not determine', which callers must treat as unknown, never as absent."""
    for cmd in (["dig", "+short", rrtype, name], ["host", "-t", rrtype, name]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0 and r.stdout.strip():
                return [ln.strip().rstrip(".").lower() for ln in r.stdout.splitlines() if ln.strip()]
        except Exception:
            continue
    try:
        import dns.resolver  # type: ignore
        return [str(a).rstrip(".").lower() for a in dns.resolver.resolve(name, rrtype)]
    except Exception:
        return []


def registrable_domain(hostname: str) -> str:
    """mongoose.example.org -> example.org. Good enough for the common cases this
    flow meets; a wrong split only weakens the preflight's specificity, never its safety,
    because unknown always falls through to the generic guidance."""
    parts = [p for p in (hostname or "").lower().split(".") if p]
    if len(parts) >= 3 and parts[-2] in ("co", "org", "gov", "ac", "net", "me"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else (hostname or "")


def domain_dns_preflight(hostname: str) -> dict[str, Any]:
    """Answer, BEFORE any cloudflared call, the question that otherwise stalls provisioning
    mid-flow with a raw error: is this domain's DNS actually on Cloudflare yet? And if not,
    WHERE does the nameserver change happen, named specifically (the NS records themselves
    identify the current provider, e.g. dns*.ovh.net -> the OVH control panel), plus the
    one warning that genuinely matters for a domain that also runs email: verify the MX
    records survived Cloudflare's import BEFORE switching nameservers, or mail to that
    domain stops. Unknown lookups are reported as unknown, never guessed."""
    domain = registrable_domain(hostname)
    ns = _lookup("NS", domain)
    mx = _lookup("MX", domain)
    on_cloudflare = any("cloudflare" in n for n in ns)
    provider = ""
    provider_path = ""
    joined = " ".join(ns)
    if "ovh" in joined:
        provider = "OVH"
        provider_path = "OVH control panel: Web Cloud -> Domain names -> " + domain + " -> Servers (DNS servers) -> Modify"
    elif "cloudflare" in joined:
        provider = "Cloudflare"
    elif ns:
        provider = ns[0].split(".")[-2].capitalize() if "." in ns[0] else ns[0]
    has_mail = bool(mx)
    out: dict[str, Any] = {"domain": domain, "nameservers": ns, "on_cloudflare": on_cloudflare,
                           "provider": provider, "provider_path": provider_path,
                           "has_mail": has_mail, "mx": mx, "known": bool(ns)}
    if on_cloudflare:
        out["detail"] = f"{domain} is already on Cloudflare DNS. Nothing to change anywhere else."
        return out
    if not ns:
        out["detail"] = (f"Could not determine {domain}'s nameservers from here; if Turn on "
                        "fails at the route step, the message there covers the fix.")
        return out
    where = (f"in your {provider} account ({provider_path})" if provider_path
            else (f"at {provider}" if provider else "at your domain registrar"))
    mail_warning = ""
    if has_mail:
        mail_warning = (f" IMPORTANT because {domain} also runs email ({mx[0].split()[-1] if mx else 'MX present'}): "
                       "after adding the site to Cloudflare, check the imported DNS list "
                       "shows the MX records BEFORE you switch the nameservers, or email to "
                       f"@{domain} addresses stops working.")
    out["detail"] = (f"{domain} is currently on {provider or 'another provider'}'s nameservers, "
                    f"not Cloudflare's. One-time change {where}: first add {domain} as a site "
                    "in your Cloudflare dashboard (free) and let it import the existing DNS "
                    "records; then swap the nameservers to the two Cloudflare shows you. Your "
                    f"website is untouched (Mongoose only adds the {hostname} subdomain)."
                    + mail_warning)
    return out


def provision(hostname: str) -> dict[str, Any]:
    """Everything after login, as one action behind one button: create the tunnel if
    needed, route the DNS name to it, write the ingress config pointed at THIS Mongoose,
    start the supervised tunnel process, and persist the env keys so every surface
    switches to the new address. Each step reports individually so a failure names exactly
    where it stopped instead of a generic 'provisioning failed'."""
    hostname = (hostname or "").strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
    if not hostname or "." not in hostname:
        return {"ok": False, "error": "give a real hostname, like mongoose.example.com"}
    binary = cloudflared_path()
    if not binary:
        return {"ok": False, "step": "install", "error": "cloudflared is not installed"}
    if not _logged_in():
        return {"ok": False, "step": "login", "error": "not logged in to Cloudflare yet"}
    # DNS preflight BEFORE any cloudflared call: if the domain's nameservers aren't
    # Cloudflare's yet, every later step is doomed, and this names the exact place the
    # change happens (e.g. the OVH control panel) plus the email-records warning, instead
    # of stalling minutes later on cloudflared's raw zone error. An unknown lookup result
    # deliberately does NOT block: the route step's own error mapping still covers it.
    pre = domain_dns_preflight(hostname)
    if pre.get("known") and not pre.get("on_cloudflare"):
        return {"ok": False, "step": "dns_prereq", "error": pre["detail"], "preflight": pre}
    steps: dict[str, Any] = {}
    if not _tunnel_credentials_present():
        rc, out = _run(binary, "tunnel", "create", "mongoose")
        already = "already exists" in out
        steps["create"] = {"ok": rc == 0 or already, "output": out[-300:]}
        if not steps["create"]["ok"]:
            return {"ok": False, "step": "create", "error": out[-300:], "steps": steps}
        # Record OUR tunnel's UUID so identity never again means "first json file found".
        import re as _re
        m = _re.search(r"id\s+([0-9a-f-]{36})", out)
        uuid = m.group(1) if m else ""
        if not uuid:
            rc2, out2 = _run(binary, "tunnel", "list")
            for line in out2.splitlines():
                if " mongoose " in f" {line} " or line.strip().endswith(" mongoose"):
                    m2 = _re.search(r"([0-9a-f-]{36})", line)
                    if m2:
                        uuid = m2.group(1)
                        break
        if not uuid:
            return {"ok": False, "step": "create",
                    "error": "tunnel created but its UUID could not be determined; run again",
                    "steps": steps}
        (_managed_config_dir() / "tunnel_uuid").write_text(uuid)
    rc, out = _run(binary, "tunnel", "route", "dns", "mongoose", hostname)
    route_already = rc != 0 and "already" in out.lower()
    steps["route"] = {"ok": rc == 0 or route_already, "already_existed": route_already,
                      "output": out[-300:]}
    if not steps["route"]["ok"]:
        # The single most likely stall for a real domain, made self-explanatory: routing a
        # DNS name to the tunnel only works if the domain's ZONE is already on Cloudflare
        # DNS in this account. cloudflared's raw error ("zone not found" and variants) does
        # not tell the person the actual fix, and for a domain hosting a live site the fix
        # must also say what is and is not at risk: adding the domain to Cloudflare imports
        # the existing records, and using a SUBDOMAIN for Mongoose leaves the live site
        # untouched.
        low = out.lower()
        if "zone" in low or "not found" in low or "failed to find" in low:
            domain = ".".join(hostname.split(".")[-2:])
            return {"ok": False, "step": "route",
                    "error": (f"{domain} is not on Cloudflare DNS yet (or not in the account "
                              "you approved). One-time fix, and your existing website stays "
                              f"untouched because Mongoose only uses a subdomain: add {domain} "
                              "as a site in your Cloudflare dashboard (free plan), let "
                              "Cloudflare import the existing DNS records, then change the "
                              "nameservers at your domain registrar to the two Cloudflare "
                              "gives you. Once the site shows Active in Cloudflare, press "
                              "Turn on here again."),
                    "steps": steps}
        return {"ok": False, "step": "route", "error": out[-300:], "steps": steps}
    try:
        from .settings import get_settings
        port = int(getattr(get_settings(), "port", 8765) or 8765)
        uuid = _recorded_tunnel_uuid()
        creds = _cloudflared_dir() / f"{uuid}.json"
        cfg = (f"tunnel: {uuid}\n"
               f"credentials-file: {creds}\n"
               f"ingress:\n"
               f"  - hostname: {hostname}\n"
               f"    service: https://127.0.0.1:{port}\n"
               f"    originRequest:\n"
               f"      noTLSVerify: true\n"
               f"  - service: http_status:404\n")
        (_managed_config_dir() / "config.yml").write_text(cfg)
        steps["config"] = {"ok": True}
    except Exception as exc:
        return {"ok": False, "step": "config", "error": str(exc)[:300], "steps": steps}
    steps["run"] = ensure_tunnel_running()
    saved = set_tunnel_hostname(hostname)
    steps["env"] = saved
    if not (steps["run"].get("ok") and saved.get("ok")):
        return {"ok": False, "step": "run" if not steps["run"].get("ok") else "env",
                "error": steps["run"].get("error") or saved.get("error") or "could not start",
                "hostname": hostname, "public_url": f"https://{hostname}", "steps": steps}
    # Audit finding, conceded: returning success because the local process started let the
    # UI say "Live" before anything proved a guest can actually get through, which is the
    # DDNS configured-is-not-reachable mistake in new clothes. Success now requires the
    # end-to-end proof; a started-but-unproven tunnel returns status configured_not_proven
    # honestly (DNS propagation right after routing can legitimately take a short while).
    import time as _time
    proof: dict[str, Any] = {"ok": False}
    for _ in range(3):
        proof = tunnel_end_to_end_proof(hostname)
        if proof.get("ok"):
            break
        _time.sleep(2)
    steps["proof"] = proof
    if proof.get("ok"):
        return {"ok": True, "status": "live", "hostname": hostname,
                "public_url": f"https://{hostname}", "steps": steps}
    if steps.get("route", {}).get("already_existed"):
        # The mixed-setup trap, found via a parallel assistant's terminal instructions: if
        # a tunnel for this hostname was ever set up another way (Zero Trust dashboard, or
        # `cloudflared tunnel route dns` by hand), the DNS record already exists and points
        # at THAT tunnel, not this one. Cloudflare reports "already exists", the route step
        # can't tell whose record it is, and blaming propagation here would send the owner
        # into an infinite wait on something that will never resolve.
        return {"ok": False, "status": "configured_not_proven", "step": "proof",
                "error": (f"{hostname} already had a DNS record before this setup, and the "
                         "public address is not answering as this Mongoose. Most likely the "
                         "name is still routed to a DIFFERENT tunnel (for example one made "
                         "earlier via the Cloudflare dashboard or terminal commands). Fix: in "
                         "the Cloudflare dashboard, open the domain's DNS records, delete the "
                         f"CNAME for {hostname}, then press Turn on here again."),
                "hostname": hostname, "public_url": f"https://{hostname}", "steps": steps}
    return {"ok": False, "status": "configured_not_proven", "step": "proof",
            "error": ("configured and started, but the public address hasn't answered as this "
                     "Mongoose yet (DNS can take a few minutes right after setup; use the "
                     "card's check again shortly): " + str(proof.get("error") or "no answer")),
            "hostname": hostname, "public_url": f"https://{hostname}", "steps": steps}


def cloudflared_path() -> str | None:
    """Where the cloudflared binary is, or None. Checks PATH first, then the standard
    Homebrew locations, since a GUI-launched process on macOS often lacks brew's PATH."""
    p = shutil.which("cloudflared")
    if p:
        return p
    for candidate in ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"):
        if Path(candidate).exists():
            return candidate
    try:
        managed = _managed_bin_dir() / "cloudflared"
        if managed.exists():
            return str(managed)
    except Exception:
        pass
    return None


def _cloudflared_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".cloudflared"


def _logged_in() -> bool:
    """cloudflared login drops cert.pem in ~/.cloudflared; its presence is the practical
    signal that this machine is authorized against a Cloudflare account."""
    return (_cloudflared_dir() / "cert.pem").exists()


def _managed_config_dir() -> Path:
    """Mongoose's OWN cloudflared home: data/cloudflared. Audit finding: treating any
    ~/.cloudflared/*.json as 'our tunnel', any cloudflared process as 'our process', and
    overwriting the user-level ~/.cloudflared/config.yml all break the moment the machine
    has any pre-existing Cloudflare Tunnel use, and can show a false success on someone
    else's tunnel. Everything Mongoose creates now lives under its own data dir; the only
    thing shared with ~/.cloudflared is cert.pem, which is account auth, genuinely global."""
    from .settings import get_settings
    d = get_settings().data_dir / "cloudflared"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_file() -> Path | None:
    p = _managed_config_dir() / "config.yml"
    return p if p.exists() else None


def _recorded_tunnel_uuid() -> str:
    try:
        return (_managed_config_dir() / "tunnel_uuid").read_text().strip()
    except Exception:
        return ""


def _tunnel_credentials_present() -> bool:
    """OUR tunnel exists: a recorded UUID whose credentials file is actually on disk. Any
    stray <uuid>.json from someone else's tunnel no longer counts."""
    uuid = _recorded_tunnel_uuid()
    if not uuid:
        return False
    return (_cloudflared_dir() / f"{uuid}.json").exists()


def _process_running() -> bool:
    """OUR tunnel process is running: the pid Mongoose recorded is alive AND its command
    line references Mongoose's own config path. `pgrep -f cloudflared` matched any
    cloudflared on the machine, including tunnels Mongoose never started."""
    try:
        pid = int(_tunnel_pidfile().read_text().strip())
    except Exception:
        return False
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return False
        cmd = (r.stdout or "").strip()
        return "cloudflared" in cmd and str(_managed_config_dir()) in cmd
    except Exception:
        return False


def _reboot_persistent() -> bool:
    """Is the SERVER's user-level LaunchAgent both installed AND loaded? Two traps this
    dodges, both found the hard way: (1) the codebase has TWO LaunchAgents, and the
    `mongoose service` one only starts the SCHEDULER, not the server; the one that brings
    the server back after a reboot is com.mongoose.server-watchdog (RunAtLoad + KeepAlive
    keeps the watchdog alive, and the watchdog keeps the server alive). (2) plist-on-disk
    alone is not enough: the forensics on the real machine showed exactly that split
    (plist present, launchctl list empty), a setup that dies at the next reboot."""
    try:
        from pathlib import Path as _P
        label = "com.mongoose.server-watchdog"
        plist = _P.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        if not plist.exists():
            return False
        r = subprocess.run(["launchctl", "list", label], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def tunnel_state(env: dict | None = None) -> dict[str, Any]:
    """Every fact about the tunnel that can be read from THIS machine, each independently
    checked (no fact inferred from another; a config file existing does not mean the
    process is running, and vice versa)."""
    if env is None:
        try:
            from .setup_wizard import effective_env
            env = effective_env()
        except Exception:
            env = dict(os.environ)
    hostname = (env.get(ENV_HOSTNAME_KEY) or "").strip()
    provider = (env.get(ENV_PROVIDER_KEY) or "").strip().lower()
    binary = cloudflared_path()
    return {
        "provider": provider,
        "hostname": hostname,
        "configured": provider == "cloudflare" and bool(hostname),
        "binary": binary or "",
        "installed": bool(binary),
        "logged_in": _logged_in(),
        "tunnel_created": _tunnel_credentials_present(),
        "config_file": str(_config_file() or ""),
        "process_running": _process_running(),
        # A precise external criticism of an earlier claim forced this fact: Mongoose
        # supervising cloudflared means the tunnel returns whenever MONGOOSE returns, but
        # that only "survives reboots" if something actually starts Mongoose after a
        # reboot. Without a loaded user-level LaunchAgent the honest reading is "survives
        # tunnel crashes during a Mongoose session", and the UI must say so rather than
        # show Live on a setup that dies at the next restart.
        "reboot_persistent": _reboot_persistent(),
        "cloudflared_boot_agent": cloudflared_boot_agent_status(),
    }


def _looks_like_dns_failure(msg: str) -> bool:
    """True when an HTTP error is the LOCAL resolver failing to find the name, not the
    remote route being down. These are the getaddrinfo failure strings across platforms."""
    m = (msg or "").lower()
    return any(s in m for s in (
        "nodename nor servname", "name or service not known", "getaddrinfo",
        "temporary failure in name resolution", "could not resolve", "no address associated"))


def _public_dns_resolve(hostname: str, timeout: float = 5.0) -> str | None:
    """Resolve hostname to an A record via Cloudflare DoH over the 1.1.1.1 IP literal, so a
    lagging or filtering LOCAL resolver (NextDNS, Pi-hole, corporate DNS) cannot make a
    genuinely-live guest route look dead. The 1.1.1.1 endpoint needs no DNS of its own and
    its certificate carries the 1.1.1.1 IP SAN, so this works even when local DNS is broken."""
    import urllib.request
    req = urllib.request.Request(
        f"https://1.1.1.1/dns-query?name={hostname}&type=A",
        headers={"accept": "application/dns-json", "User-Agent": "mongoose-tunnel-proof"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read(8192).decode("utf-8", "replace"))
        for ans in data.get("Answer", []):
            if ans.get("type") == 1 and ans.get("data"):  # type 1 == A record
                return str(ans["data"])
    except Exception:
        return None
    return None


def _https_get_health(hostname: str, timeout: float, connect_ip: str | None = None):
    """GET https://<hostname>/health and return (status, body). When connect_ip is given,
    connect to that IP while still presenting SNI and Host = hostname, so an IP resolved via
    public DNS still routes correctly through Cloudflare to the right tunnel."""
    import urllib.request
    if connect_ip is None:
        req = urllib.request.Request(f"https://{hostname}/health",
                                     headers={"User-Agent": "mongoose-tunnel-proof"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(4096).decode("utf-8", "replace")
    import http.client, socket, ssl
    ctx = ssl.create_default_context()
    raw = socket.create_connection((connect_ip, 443), timeout=timeout)
    try:
        tls = ctx.wrap_socket(raw, server_hostname=hostname)  # SNI = hostname, not the IP
        conn = http.client.HTTPConnection(hostname, timeout=timeout)
        conn.sock = tls
        conn.request("GET", "/health",
                     headers={"Host": hostname, "User-Agent": "mongoose-tunnel-proof"})
        resp = conn.getresponse()
        return resp.status, resp.read(4096).decode("utf-8", "replace")
    finally:
        try:
            raw.close()
        except Exception:
            pass


def tunnel_end_to_end_proof(hostname: str, timeout: float = 8.0) -> dict[str, Any]:
    """A real HTTPS GET to https://<hostname>/health over the public internet. Unlike the
    DDNS self-test, this exercises the exact external path a guest's phone uses (out to
    Cloudflare's edge, back down the tunnel), so success here is genuine end-to-end proof,
    and the response body is checked to be THIS product's health JSON, not merely 'something
    answered on that name' (a parked page or someone else's service must not count).

    The check is NOT hostage to this Mac's resolver: if the local DNS cannot find the name
    (a real case, e.g. NextDNS still syncing the new record across its nodes) we re-resolve
    via public DNS and connect to that IP, because what matters is whether a GUEST can reach
    the route, not whether the owner's filtered resolver has caught up yet."""
    out: dict[str, Any] = {"ok": False, "hostname": hostname, "status": None, "is_mongoose": False}
    if not hostname:
        out["error"] = "no hostname configured"
        return out

    def _finish(status: int, body: str, resolved_via: str) -> dict[str, Any]:
        out["status"] = status
        out["resolved_via"] = resolved_via
        try:
            data = json.loads(body)
            out["is_mongoose"] = (data.get("name") == "mongoose")
            out["version"] = data.get("version")
        except Exception:
            out["is_mongoose"] = False
        out["ok"] = bool(status == 200 and out["is_mongoose"])
        if status == 200 and not out["is_mongoose"]:
            out["error"] = "something answered on that hostname, but it is not this Mongoose"
        return out

    try:
        status, body = _https_get_health(hostname, timeout)
        return _finish(status, body, "local")
    except Exception as exc:
        msg = str(exc)
        if _looks_like_dns_failure(msg):
            # Local resolver could not find the name. Ask public DNS the way a guest would.
            ip = _public_dns_resolve(hostname)
            if ip:
                try:
                    status, body = _https_get_health(hostname, timeout, connect_ip=ip)
                    return _finish(status, body, "public-dns")
                except Exception as exc2:
                    msg = str(exc2)
            else:
                out["error"] = ("This Mac cannot resolve the guest hostname yet (local "
                    "resolver lag or filtering), and public DNS did not return it either. "
                    "Real guests may still be fine once DNS finishes propagating.")
                return out
        msg = msg[:200]
        # A 502 from Cloudflare's edge means the tunnel is up but the LOCAL hop from
        # cloudflared to Mongoose failed. With Mongoose-managed config that is usually
        # Mongoose itself not answering on the loopback origin, and occasionally the origin
        # TLS hop (noTLSVerify is written automatically, but a hand-edited config can lose
        # it). Name both rather than the bare status code, but do not claim TLS specifically.
        if "502" in msg:
            out["status"] = 502
            msg = ("Cloudflare's edge answered 502: the tunnel is up, but the hop from "
                  "cloudflared to Mongoose on this Mac failed. Usually Mongoose isn't "
                  "answering on its local port; if the tunnel config was hand-edited, also "
                  "check the origin still has noTLSVerify for Mongoose's self-signed "
                  "certificate.")
        out["error"] = msg
        return out


def setup_plan(env: dict | None = None) -> list[dict[str, Any]]:
    """The ordered guided journey, each step with a live done-check so re-running always
    shows exactly where you are, with the exact command for whichever step is next. The
    ingress must point at the LOOPBACK https origin (Mongoose serves TLS itself), so the
    config includes noTLSVerify for the self-signed local hop; the public hop gets a real
    trusted Cloudflare certificate regardless."""
    st = tunnel_state(env)
    hostname = st["hostname"] or "mongoose.YOURDOMAIN.com"
    try:
        from .settings import get_settings
        port = int(getattr(get_settings(), "port", 8765) or 8765)
    except Exception:
        port = 8765
    return [
        {"step": "install", "done": st["installed"],
         "title": "Install cloudflared",
         "command": "brew install cloudflared"},
        {"step": "login", "done": st["logged_in"],
         "title": "Authorize this Mac against your Cloudflare account (opens a browser)",
         "command": "cloudflared tunnel login"},
        {"step": "create", "done": st["tunnel_created"],
         "title": "Create the tunnel",
         "command": "cloudflared tunnel create mongoose"},
        {"step": "route", "done": bool(st["hostname"]),
         "title": "Point your hostname at the tunnel",
         "command": f"cloudflared tunnel route dns mongoose {hostname}"},
        {"step": "config", "done": bool(st["config_file"]),
         "title": "Write ~/.cloudflared/config.yml",
         "command": ("cat > ~/.cloudflared/config.yml <<'EOF'\n"
                     "tunnel: mongoose\n"
                     f"credentials-file: {_cloudflared_dir()}/<TUNNEL-UUID>.json\n"
                     "ingress:\n"
                     f"  - hostname: {hostname}\n"
                     f"    service: https://127.0.0.1:{port}\n"
                     "    originRequest:\n"
                     "      noTLSVerify: true\n"
                     "  - service: http_status:404\n"
                     "EOF")},
        {"step": "service", "done": st["process_running"],
         "title": "Run it as a service that survives reboots",
         "command": "sudo cloudflared service install && sudo cloudflared service start"},
        {"step": "env", "done": st["configured"],
         "title": "Tell Mongoose the tunnel is its public address",
         "command": f"mongoose tunnel set-hostname {hostname}"},
    ]


def set_tunnel_hostname(hostname: str) -> dict[str, Any]:
    """Persist tunnel mode: the tunnel keys AND MONGOOSE_PUBLIC_URL together, so every
    existing surface (pairing, launchpad, PWA origin, guest invites, remote_away) switches
    to the tunnel hostname with no further steps. No port suffix: the whole point is a
    boring, normal https URL on 443 that works on any phone and any network."""
    hostname = (hostname or "").strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
    if not hostname or "." not in hostname:
        return {"ok": False, "error": "give a real hostname, like mongoose.example.com"}
    from .setup_wizard import write_env
    write_env({
        ENV_PROVIDER_KEY: "cloudflare",
        ENV_HOSTNAME_KEY: hostname,
        "MONGOOSE_PUBLIC_URL": f"https://{hostname}",
    })
    return {"ok": True, "hostname": hostname, "public_url": f"https://{hostname}"}
