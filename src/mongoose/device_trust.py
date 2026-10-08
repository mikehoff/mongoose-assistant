"""Device trust + HTTPS for LAN.

Browser microphone capture (voice on a phone/tablet) requires HTTPS with a certificate the device
TRUSTS. There are two honest paths:

  1. mkcert (preferred): creates a local Certificate Authority and installs it into THIS Mac's trust
     store automatically. For a phone/tablet to trust certs from that CA, the CA's root certificate
     must be installed on the device. mongoose can GENERATE the cert, SERVE the CA root for download
     (with a QR code), and give the EXACT per-device steps, but the final "install + enable trust"
     tap happens in the device's Settings. That is an OS security boundary (iOS/Android both require an
     explicit user action to trust a new root CA); no app can or should bypass it.

  2. self-signed (fallback, no mkcert): zero install, but every device shows a one-time warning the
     user must accept. Works, just less clean.

This module automates everything up to the unavoidable device tap, and makes that tap a guided, QR-led,
copy-paste-free experience instead of a terminal chore.
"""
from __future__ import annotations
import os as _os

def _serve_port() -> str:
    """Actual Mongoose serving port: respects MONGOOSE_PORT env."""
    try:
        from .settings import get_settings
        p = getattr(get_settings(), "port", None)
        return str(p) if p else _os.environ.get("MONGOOSE_PORT", "8765")
    except Exception:
        return _os.environ.get("MONGOOSE_PORT", "8765")


import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _cert_dir() -> Path:
    base = Path(os.path.expanduser(os.getenv("MONGOOSE_DATA_DIR", "~/.mongoose/data")))
    d = base / "certs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lan_ip() -> str:
    """Best-effort primary LAN IP (macOS + Linux), falls back to 127.0.0.1."""
    for cmd in (["ipconfig", "getifaddr", "en0"], ["hostname", "-I"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=4).stdout.strip()
            if out:
                return out.split()[0]
        except Exception:
            continue
    return "127.0.0.1"


def trust_status() -> dict[str, Any]:
    """Where the device-trust setup stands right now. Pure read."""
    cert = _cert_dir() / "mongoose.pem"
    key = _cert_dir() / "mongoose-key.pem"
    has_mkcert = bool(shutil.which("mkcert"))
    caroot = ""
    if has_mkcert:
        try:
            caroot = subprocess.run(["mkcert", "-CAROOT"], capture_output=True, text=True,
                                    timeout=5).stdout.strip()
        except Exception:
            caroot = ""
    ca_file = str(Path(caroot) / "rootCA.pem") if caroot else ""
    return {
        "cert_exists": cert.exists() and key.exists(),
        "cert_path": str(cert),
        "key_path": str(key),
        "mkcert_available": has_mkcert,
        "method": "mkcert" if has_mkcert else "self-signed",
        "ca_root_dir": caroot,
        "ca_file": ca_file if ca_file and Path(ca_file).exists() else "",
        "lan_ip": _lan_ip(),
        "tls_configured": bool(os.getenv("MONGOOSE_TLS_CERTFILE") and os.getenv("MONGOOSE_TLS_KEYFILE")),
    }


def generate_cert(*extra_names: str) -> dict[str, Any]:
    """Generate the HTTPS cert+key for localhost + this Mac's LAN IP (+ any extra hostnames). Uses
    mkcert when available (trusted CA, no browser warnings once the CA is on the device), else a
    self-signed cert. Persists MONGOOSE_TLS_CERTFILE/KEYFILE so the next `serve` picks it up. Returns
    the paths and which method was used, so the caller can route the device-trust step appropriately."""
    cert = _cert_dir() / "mongoose.pem"
    key = _cert_dir() / "mongoose-key.pem"
    lan = _lan_ip()
    names = ["localhost", "127.0.0.1", lan, *[n for n in extra_names if n]]

    if shutil.which("mkcert"):
        try:
            # Install the local CA into THIS machine's trust store (idempotent, no-op if already there).
            subprocess.run(["mkcert", "-install"], capture_output=True, text=True, timeout=30)
            subprocess.run(["mkcert", "-cert-file", str(cert), "-key-file", str(key), *names],
                           capture_output=True, text=True, timeout=30, check=True)
            method = "mkcert"
        except subprocess.CalledProcessError as exc:
            return {"ok": False, "error": f"mkcert failed: {exc.stderr.strip()[:160]}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"mkcert error: {exc}"}
    else:
        # self-signed fallback via openssl
        san = ",".join(
            (f"IP:{n}" if _looks_ip(n) else f"DNS:{n}") for n in names
        )
        try:
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "825",
                 "-keyout", str(key), "-out", str(cert),
                 "-subj", "/CN=mongoose.local", "-addext", f"subjectAltName={san}"],
                capture_output=True, text=True, timeout=30, check=True)
            method = "self-signed"
        except FileNotFoundError:
            return {"ok": False, "error": "Neither mkcert nor openssl is available to make a cert."}
        except subprocess.CalledProcessError as exc:
            return {"ok": False, "error": f"openssl failed: {exc.stderr.strip()[:160]}"}

    try:
        os.chmod(key, 0o600)
    except Exception:
        pass
    _persist_tls(str(cert), str(key))
    return {"ok": True, "method": method, "cert_path": str(cert), "key_path": str(key),
            "names": names, "lan_ip": lan}


def _looks_ip(s: str) -> bool:
    parts = s.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)


def _persist_tls(cert: str, key: str) -> None:
    try:
        from .setup_wizard import write_env
        write_env({"MONGOOSE_TLS_CERTFILE": cert, "MONGOOSE_TLS_KEYFILE": key})
        os.environ["MONGOOSE_TLS_CERTFILE"] = cert
        os.environ["MONGOOSE_TLS_KEYFILE"] = key
        import mongoose.settings as _S
        if hasattr(_S, "_settings"):
            _S._settings = None
        if hasattr(_S.get_settings, "cache_clear"):
            _S.get_settings.cache_clear()
    except Exception:
        pass


def device_trust_plan(platform: str = "ios") -> dict[str, Any]:
    """The delightful, honest device-trust guide. Returns the CA download URL (served by the cockpit),
    a QR target, and the EXACT per-platform steps for the one unavoidable manual action. We never claim
    to bypass the trust prompt, we make reaching it a two-tap, QR-led path."""
    st = trust_status()
    lan = st["lan_ip"]
    # The cockpit serves the CA at this path (added in app.py). On self-signed, there's no CA to install,
    # the device just accepts the cert warning once.
    ca_url = f"https://{lan}:{_serve_port()}/trust/ca.pem" if st["method"] == "mkcert" else ""
    if st["method"] != "mkcert":
        return {
            "method": "self-signed",
            "needs_ca_install": False,
            "summary": "No CA to install. The first time you open the cockpit on the device over https, "
                       "accept the one-time certificate warning, then the microphone works.",
            "steps": [
                f"On the device, open https://{lan}:{_serve_port()}/ui",
                "Accept the certificate warning once (Advanced, then proceed).",
                "The mic now works for voice in the browser.",
            ],
        }

    ios = [
        "Scan the QR (or open the link) on the device to download Mongoose's CA certificate.",
        "iOS will say the profile was downloaded. Open Settings, it shows 'Profile Downloaded' near the top.",
        "Tap it, then Install (top right), enter your passcode, Install again.",
        "Now the crucial trust step: Settings, General, About, Certificate Trust Settings.",
        "Turn ON the switch next to the 'mkcert' / Mongoose root certificate.",
        f"Open https://{lan}:{_serve_port()}/ui, no warning, and the mic works.",
    ]
    android = [
        "Scan the QR (or open the link) to download Mongoose's CA certificate.",
        "Settings, Security, More security settings, Encryption & credentials, Install a certificate.",
        "Choose 'CA certificate', then select the downloaded rootCA.pem.",
        "Confirm 'Install anyway' if warned.",
        f"Open https://{lan}:{_serve_port()}/ui, no warning, and the mic works.",
    ]
    macos = [
        "On the Mac the CA is already trusted (mkcert -install did this automatically).",
        f"Just open https://{lan}:{_serve_port()}/ui, the mic works.",
    ]
    steps = {"ios": ios, "android": android, "macos": macos}.get(platform, ios)
    # Render the QR locally (reusing the pairing QR helper) so NOTHING is fetched from outside the LAN.
    qr_svg = None
    if ca_url:
        try:
            from .devices import pairing_qr_svg
            qr_svg = pairing_qr_svg(ca_url)
        except Exception:
            qr_svg = None
    return {
        "method": "mkcert",
        "needs_ca_install": platform in ("ios", "android"),
        "ca_url": ca_url,
        "qr_target": ca_url,
        "qr_svg": qr_svg,
        "platform": platform,
        "summary": ("mongoose generated a trusted certificate. Install its CA on the device once "
                    "(scan the QR, then flip the trust switch in Settings) and every browser mic just "
                    "works, no warnings, ever again. The one tap in Settings is an Apple/Android "
                    "security rule mongoose can't and shouldn't skip."),
        "steps": steps,
    }


def setup_device_trust(*extra_names: str) -> dict[str, Any]:
    """One-call: generate the cert (trusted CA when possible), then return the device-trust plan. The
    caller (cockpit) renders the QR + steps. Idempotent, re-running just regenerates the cert."""
    gen = generate_cert(*extra_names)
    if not gen.get("ok"):
        return {"ok": False, "error": gen.get("error", "cert generation failed")}
    plan = device_trust_plan("ios")
    return {"ok": True, "method": gen["method"], "cert_path": gen["cert_path"],
            "lan_ip": gen.get("lan_ip", _lan_ip()), "plan": plan,
            "restart_needed": True,
            "detail": "Certificate ready. Restart Mongoose with TLS on (it's now configured), then "
                      "follow the device steps to trust it."}


def ca_file_bytes() -> tuple[bytes, str] | None:
    """The CA root certificate bytes for the /trust/ca.pem download, or None if there's no CA (self-
    signed mode has no separate CA to install)."""
    st = trust_status()
    if not st.get("ca_file"):
        return None
    try:
        with open(st["ca_file"], "rb") as f:
            return f.read(), "rootCA.pem"
    except Exception:
        return None
