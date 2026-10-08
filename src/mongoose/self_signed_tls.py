"""One-click LAN HTTPS. Generates a self-signed certificate in-process (using the `cryptography`
library, so no openssl/mkcert/certbot dependency and no shell required) covering localhost, the
Mac's LAN IP, and any hostnames. This is what unlocks the phone microphone, since browsers only
expose getUserMedia in a secure context (https or localhost).

This is the zero-friction path: the cockpit can call generate_self_signed() with one button, write
the cert paths into .env, and the user restarts. Devices show a one-time trust warning (self-signed),
which we explain clearly. For a warning-free experience the existing acme_tls.py (Let's Encrypt via
DuckDNS) remains the option for users with a public domain."""
from __future__ import annotations

import datetime
import ipaddress
from pathlib import Path
from typing import Any

from .settings import get_settings


def _cert_dir() -> Path:
    d = get_settings().data_dir / "certs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _primary_lan_ip() -> str:
    """Best-effort primary LAN IP of this machine."""
    try:
        from .discovery import _primary_ip
        ip = _primary_ip()
        if ip:
            return ip
    except Exception:
        pass
    # fallback: ask the OS which source IP it'd use to reach a public address (no packets sent)
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def cert_paths() -> tuple[Path, Path]:
    return _cert_dir() / "mongoose.pem", _cert_dir() / "mongoose-key.pem"


def generate_self_signed(extra_hostnames: list[str] | None = None) -> dict[str, Any]:
    """Generate a self-signed cert+key covering localhost, 127.0.0.1, this Mac's LAN IP, and any
    extra hostnames. Writes them to data/certs and points .env at them. Returns a structured result.
    Idempotent in effect: regenerating simply overwrites with a fresh 2-year cert."""
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except Exception as exc:
        return {"ok": False, "error": f"cryptography library unavailable: {exc}"}

    lan_ip = _primary_lan_ip()
    hostnames = ["localhost"]
    for h in (extra_hostnames or []):
        h = (h or "").strip()
        if h and h not in hostnames:
            hostnames.append(h)
    # mongoose.local is a common mDNS name; include it so http://mongoose.local works too
    if "mongoose.local" not in hostnames:
        hostnames.append("mongoose.local")

    ip_addrs = []
    for ip in ["127.0.0.1", "::1", lan_ip]:
        try:
            ip_addrs.append(ipaddress.ip_address(ip))
        except ValueError:
            pass

    san_list: list = [x509.DNSName(h) for h in hostnames]
    san_list += [x509.IPAddress(ip) for ip in ip_addrs]

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Mongoose Local"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Mongoose"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=730))  # 2 years
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path, key_path = cert_paths()
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    try:
        key_path.chmod(0o600)
    except OSError:
        pass

    # Point .env at the cert so `mongoose serve` auto-enables https on next start.
    try:
        from .setup_wizard import write_env
        write_env({"MONGOOSE_TLS_CERTFILE": str(cert_path),
                   "MONGOOSE_TLS_KEYFILE": str(key_path)})
        env_written = True
    except Exception:
        env_written = False

    return {
        "ok": True,
        "certfile": str(cert_path),
        "keyfile": str(key_path),
        "hostnames": hostnames,
        "ip_addresses": [str(ip) for ip in ip_addrs],
        "lan_ip": lan_ip,
        "env_written": env_written,
        "expires": (now + datetime.timedelta(days=730)).strftime("%Y-%m-%d"),
        "detail": "Self-signed certificate generated. Restart Mongoose to serve HTTPS, then open "
                  f"https://{lan_ip}:{get_settings().port} on your phone (accept the one-time "
                  "security warning) to use voice.",
        "self_signed": True,
    }


def self_signed_status() -> dict[str, Any]:
    """Is there a self-signed cert in place, and what does it cover? Read-only, no external deps."""
    cert_path, key_path = cert_paths()
    if not cert_path.exists() or not key_path.exists():
        return {"present": False, "lan_ip": _primary_lan_ip(), "port": get_settings().port}
    info: dict[str, Any] = {"present": True, "certfile": str(cert_path),
                            "lan_ip": _primary_lan_ip(), "port": get_settings().port}
    try:
        from cryptography import x509
        data = cert_path.read_bytes()
        cert = x509.load_pem_x509_certificate(data)
        not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
        # normalise to naive UTC for a consistent comparison
        na = not_after.replace(tzinfo=None) if getattr(not_after, "tzinfo", None) else not_after
        info["expires"] = na.strftime("%Y-%m-%d")
        info["expired"] = na < datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            names = [g.value if hasattr(g, "value") else str(g) for g in san]
            info["covers"] = [str(n) for n in names]
        except Exception:
            info["covers"] = []
        # is .env actually pointing at this cert (i.e. will https be served on restart)?
        s = get_settings()
        info["configured"] = (getattr(s, "tls_certfile", "") == str(cert_path)
                              and getattr(s, "tls_keyfile", "") == str(key_path))
        # is the running process actually serving https right now?
        info["serving_https"] = bool(getattr(s, "tls_certfile", "") and getattr(s, "tls_keyfile", ""))
    except Exception as exc:
        info["parse_error"] = str(exc)
    return info


def days_until_expiry() -> int | None:
    """Days until the self-signed cert expires, or None when no cert is present/parseable."""
    st = self_signed_status()
    if not st.get("present") or not st.get("expires"):
        return None
    try:
        na = datetime.datetime.strptime(st["expires"], "%Y-%m-%d")
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        return (na - now).days
    except Exception:
        return None


def renew_if_expiring(*, days_threshold: int = 30) -> dict[str, Any]:
    """Regenerate the self-signed cert in place when it is within days_threshold of expiry
    (or already expired), preserving the extra hostnames the current cert covers.

    Why this exists: the cert is minted once with a 2-year life. Without renewal the phone
    cockpit one day meets a hard browser refusal with no explanation. The new cert is written
    to the same paths, so the next server restart (updates restart the server anyway) serves it.
    Read-only when the cert is healthy."""
    st = self_signed_status()
    if not st.get("present"):
        return {"ok": True, "skipped": "no self-signed cert in place"}
    remaining = days_until_expiry()
    if remaining is None:
        return {"ok": False, "error": "could not parse the existing cert's expiry"}
    if remaining > days_threshold:
        return {"ok": True, "skipped": f"cert healthy ({remaining} days remaining)"}
    # Preserve any extra SAN hostnames beyond the defaults (localhost + current LAN IP are
    # re-derived fresh, which also heals a cert minted on an old LAN IP).
    defaults = {"localhost", "127.0.0.1", "::1", _primary_lan_ip()}
    extra = [n for n in (st.get("covers") or []) if n and n not in defaults]
    res = generate_self_signed(extra_hostnames=extra or None)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "regeneration failed")}
    return {"ok": True, "renewed": True, "was_days_remaining": remaining,
            "covers": res.get("hostnames", []) + res.get("ip_addresses", []),
            "note": "New cert written in place; the next server restart serves it."}
