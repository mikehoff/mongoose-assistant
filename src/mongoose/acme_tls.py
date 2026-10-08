"""Let's Encrypt TLS for guest HTTPS: real certificates, no browser warnings.

Why this exists: guests reaching Mongoose from outside the home need HTTPS that their phone trusts
with no manual step. Self-signed/mkcert certs require installing a root CA on every guest device,
which is a non-starter for "send Sam a link." This obtains a genuine Let's Encrypt certificate for the
DuckDNS hostname so any browser trusts it out of the box.

How: the DNS-01 ACME challenge. certbot asks us to publish a TXT record at
_acme-challenge.<host>.duckdns.org; DuckDNS's API sets TXT records directly; Let's Encrypt verifies
it and issues the cert. DNS-01 needs NO inbound port (unlike HTTP-01), so it works behind NAT even
before the router port-forward is set up: which is also why it's the right challenge for a home box.

This module orchestrates certbot via its manual auth/cleanup hooks (certbot is pip-installable). It
never invents crypto; it drives the standard ACME client. On success it writes the cert/key paths into
.env (MONGOOSE_TLS_CERTFILE/KEYFILE) so `mongoose serve` picks them up. Renewal reuses the same path.

Honest boundaries: this requires the DuckDNS provider + token to be configured. For other providers a
similar DNS-01 hook would be needed; those aren't wired yet and the functions say so rather than
pretending. Issuance reaches the public ACME + DuckDNS APIs, so it can't run in an offline sandbox;
the unit tests exercise the hook logic and config wiring with the network calls mocked.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .settings import get_settings

# certbot writes here by default for a given cert name; we keep our own copy under data/ for stability.
_CERT_NAME = "mongoose"


def _duckdns_host() -> str | None:
    """The full DuckDNS hostname (e.g. 'myhome.duckdns.org') if DuckDNS is the configured provider."""
    s = get_settings()
    provider = (getattr(s, "ddns_provider", "") or os.getenv("DDNS_PROVIDER", "")).lower()
    if provider and provider != "duckdns":
        return None
    sub = os.getenv("DDNS_DUCKDNS_DOMAIN", "") or os.getenv("DUCKDNS_DOMAIN", "")
    if not sub:
        return None
    sub = sub.replace(".duckdns.org", "")
    return f"{sub}.duckdns.org"


def _duckdns_token() -> str | None:
    return os.getenv("DDNS_DUCKDNS_TOKEN", "") or os.getenv("DUCKDNS_TOKEN", "") or None


def preflight() -> dict[str, Any]:
    """Can we attempt issuance? Reports each prerequisite honestly instead of failing opaquely."""
    host = _duckdns_host()
    token = _duckdns_token()
    certbot = shutil.which("certbot")
    email = os.getenv("ACME_EMAIL", "") or os.getenv("LETSENCRYPT_EMAIL", "")
    problems = []
    if not host:
        problems.append("DuckDNS hostname not configured (set DDNS provider to duckdns + DDNS_DUCKDNS_DOMAIN).")
    if not token:
        problems.append("DuckDNS token not set (DDNS_DUCKDNS_TOKEN).")
    if not certbot:
        problems.append("certbot not installed (pip install certbot, or `mongoose tls install-certbot`).")
    if not email:
        problems.append("No ACME contact email (set ACME_EMAIL): Let's Encrypt requires one for expiry notices.")
    return {"ok": not problems, "host": host, "have_certbot": bool(certbot),
            "have_token": bool(token), "have_email": bool(email), "problems": problems}


def _set_duckdns_txt(value: str) -> bool:
    """Publish a TXT record for the ACME challenge via DuckDNS. Empty value clears it (cleanup)."""
    import urllib.request
    host = _duckdns_host()
    token = _duckdns_token()
    if not host or not token:
        return False
    sub = host.replace(".duckdns.org", "")
    clear = "&clear=true" if not value else ""
    url = f"https://www.duckdns.org/update?domains={sub}&token={token}&txt={value}{clear}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 (fixed host)
            body = resp.read().decode("utf-8", "replace").strip()
        return body.upper() == "OK"
    except Exception:
        return False


def _cert_dir() -> Path:
    return get_settings().data_dir / "certs"


def obtain_certificate(*, staging: bool = False) -> dict[str, Any]:
    """Drive certbot's DNS-01 flow to obtain a Let's Encrypt cert for the DuckDNS host, then copy the
    cert/key into data/certs and record the paths in .env. Best-effort; returns a structured result.

    staging=True uses Let's Encrypt's staging CA (untrusted but unrated-limited) for dry runs.
    """
    pf = preflight()
    if not pf["ok"]:
        return {"ok": False, "error": "; ".join(pf["problems"]), "preflight": pf}
    host = pf["host"]
    email = os.getenv("ACME_EMAIL", "") or os.getenv("LETSENCRYPT_EMAIL", "")

    # certbot manual hooks: it runs THIS module to set/clear the TXT record. We expose tiny entry
    # points via `python -m mongoose.acme_tls _auth-hook` / `_cleanup-hook` that read certbot's
    # CERTBOT_VALIDATION / CERTBOT_DOMAIN env vars.
    me = f"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}"
    auth_hook = f"PYTHONPATH={me} python3 -m mongoose.acme_tls _auth-hook"
    cleanup_hook = f"PYTHONPATH={me} python3 -m mongoose.acme_tls _cleanup-hook"

    config_dir = _cert_dir() / "certbot-config"
    work_dir = _cert_dir() / "certbot-work"
    logs_dir = _cert_dir() / "certbot-logs"
    for d in (config_dir, work_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    cmd = [
        "certbot", "certonly", "--manual", "--preferred-challenges", "dns",
        "--manual-auth-hook", auth_hook, "--manual-cleanup-hook", cleanup_hook,
        "-d", host, "--cert-name", _CERT_NAME,
        "--non-interactive", "--agree-tos", "-m", email,
        "--config-dir", str(config_dir), "--work-dir", str(work_dir), "--logs-dir", str(logs_dir),
    ]
    if staging:
        cmd.append("--staging")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        return {"ok": False, "error": "certbot not found on PATH."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "certbot timed out (DNS propagation can be slow; try again)."}
    if proc.returncode != 0:
        return {"ok": False, "error": f"certbot failed: {(proc.stderr or proc.stdout)[-400:]}"}

    live = config_dir / "live" / _CERT_NAME
    cert_src, key_src = live / "fullchain.pem", live / "privkey.pem"
    if not cert_src.exists() or not key_src.exists():
        return {"ok": False, "error": "certbot reported success but cert files are missing."}

    # Copy to stable paths and point .env at them.
    cert_dst = _cert_dir() / "fullchain.pem"
    key_dst = _cert_dir() / "privkey.pem"
    shutil.copy2(cert_src, cert_dst)
    shutil.copy2(key_src, key_dst)
    try:
        key_dst.chmod(0o600)
    except OSError:
        pass

    from .setup_wizard import write_env
    write_env({"MONGOOSE_TLS_CERTFILE": str(cert_dst), "MONGOOSE_TLS_KEYFILE": str(key_dst)})

    return {"ok": True, "host": host, "certfile": str(cert_dst), "keyfile": str(key_dst),
            "staging": staging,
            "detail": "Certificate obtained. Restart Mongoose to start serving it over HTTPS."}


def certificate_status() -> dict[str, Any]:
    """Is there a cert, and when does it expire? Read-only."""
    cert = _cert_dir() / "fullchain.pem"
    if not cert.exists():
        return {"present": False, "host": _duckdns_host()}
    try:
        # Parse the notAfter without extra deps via openssl if present, else report presence only.
        if shutil.which("openssl"):
            out = subprocess.run(["openssl", "x509", "-enddate", "-noout", "-in", str(cert)],
                                 capture_output=True, text=True, timeout=10)
            not_after = out.stdout.strip().replace("notAfter=", "") if out.returncode == 0 else ""
        else:
            not_after = ""
    except Exception:
        not_after = ""
    return {"present": True, "host": _duckdns_host(), "expires": not_after,
            "certfile": str(cert)}


# --- certbot manual hooks (invoked as subprocesses by certbot during issuance) ---

def _auth_hook() -> int:
    """certbot calls this to PUBLISH the challenge TXT record. Reads CERTBOT_VALIDATION from env."""
    validation = os.getenv("CERTBOT_VALIDATION", "")
    if not validation:
        return 1
    ok = _set_duckdns_txt(validation)
    if ok:
        # Give DNS a moment to propagate before certbot asks Let's Encrypt to check.
        import time
        time.sleep(20)
    return 0 if ok else 1


def _cleanup_hook() -> int:
    """certbot calls this to REMOVE the challenge TXT record after validation."""
    _set_duckdns_txt("")  # clear
    return 0


if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "_auth-hook":
        raise SystemExit(_auth_hook())
    if arg == "_cleanup-hook":
        raise SystemExit(_cleanup_hook())
    raise SystemExit("unknown hook")
