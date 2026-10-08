"""Tests for the Web Push (real PWA notifications) and one-click self-signed HTTPS features.
These are the pieces that make the phone experience feel like a real app and unlock voice."""
from __future__ import annotations

import base64
import os

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    import mongoose.settings as S
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    import mongoose.app as A
    return TestClient(A.app), A._COCKPIT_NONCE


def _fake_subscription():
    """A well-formed (but fake-endpoint) browser PushSubscription."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization

    def b64u(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    cp = ec.generate_private_key(ec.SECP256R1())
    cpub = cp.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": "https://fcm.googleapis.com/fcm/send/test-token",
            "keys": {"p256dh": b64u(cpub), "auth": b64u(os.urandom(16))}}, cp


# ── VAPID + subscriptions ────────────────────────────────────────────────────────────────────

def test_vapid_key_is_served(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    r = c.get("/push/vapid-key")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    # an uncompressed P-256 point is 65 bytes -> ~87 urlsafe-b64 chars
    raw = base64.urlsafe_b64decode(j["public_key"] + "==")
    assert len(raw) == 65 and raw[0] == 0x04, "VAPID public key must be an uncompressed P-256 point"


def test_subscribe_and_status(tmp_path, monkeypatch):
    c, n = _client(tmp_path, monkeypatch)
    sub, _ = _fake_subscription()
    r = c.post("/push/subscribe", json={"subscription": sub, "device_label": "iPhone"})
    assert r.status_code == 200 and r.json()["ok"] is True
    st = c.get("/cockpit/push/status", headers={"X-Cockpit-Nonce": n}).json()
    assert st["subscriptions"] == 1
    assert st["vapid_ready"] is True


def test_unsubscribe(tmp_path, monkeypatch):
    c, n = _client(tmp_path, monkeypatch)
    sub, _ = _fake_subscription()
    c.post("/push/subscribe", json={"subscription": sub})
    c.post("/push/unsubscribe", json={"endpoint": sub["endpoint"]})
    st = c.get("/cockpit/push/status", headers={"X-Cockpit-Nonce": n}).json()
    assert st["subscriptions"] == 0


# ── RFC 8291 encryption correctness (round-trip) ──────────────────────────────────────────────

def test_web_push_encryption_round_trips(tmp_path, monkeypatch):
    """The aes128gcm payload we produce must decrypt back to the original with the client's key.
    This proves the encryption is spec-correct, so a real push service would accept it."""
    _client(tmp_path, monkeypatch)  # sets up data dir + db
    from mongoose.services import web_push as WP
    WP.ensure_vapid_keys()
    sub, client_priv = _fake_subscription()

    plaintext = b'{"title":"hello","body":"world"}'
    enc = WP._encrypt_payload(plaintext, sub["keys"]["p256dh"], sub["keys"]["auth"])

    # decrypt as the browser would
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = enc[:16]
    idlen = enc[20]
    keyid = enc[21:21 + idlen]
    ct = enc[21 + idlen:]
    server_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), keyid)
    shared = client_priv.exchange(ec.ECDH(), server_pub)
    cpub = client_priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962, format=serialization.PublicFormat.UncompressedPoint)
    auth_secret = base64.urlsafe_b64decode(sub["keys"]["auth"] + "==")
    ki = b"WebPush: info\x00" + cpub + keyid
    prk = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret, info=ki).derive(shared)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(prk)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(prk)
    dec = AESGCM(cek).decrypt(nonce, ct, None).rstrip(b"\x02")
    assert dec == plaintext, "RFC 8291 encryption round-trip failed"


def test_vapid_jwt_signature_validates(tmp_path, monkeypatch):
    """The VAPID Authorization JWT must validate against the public key we advertise."""
    _client(tmp_path, monkeypatch)
    from mongoose.services import web_push as WP
    WP.ensure_vapid_keys()
    hdr = WP._vapid_auth_header("https://fcm.googleapis.com/fcm/send/x")
    assert hdr.get("Authorization", "").startswith("vapid t=")
    jwt = hdr["Authorization"].split("t=")[1].split(",")[0]
    parts = jwt.split(".")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils as U
    sig_raw = base64.urlsafe_b64decode(parts[2] + "==")
    r = int.from_bytes(sig_raw[:32], "big")
    s = int.from_bytes(sig_raw[32:], "big")
    der = U.encode_dss_signature(r, s)
    pub_raw = base64.urlsafe_b64decode(WP.public_key() + "==")
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pub_raw)
    pub.verify(der, (parts[0] + "." + parts[1]).encode(), ec.ECDSA(hashes.SHA256()))  # raises if invalid


# ── one-click self-signed TLS ─────────────────────────────────────────────────────────────────

def test_self_signed_cert_generation(tmp_path, monkeypatch):
    c, n = _client(tmp_path, monkeypatch)
    # before: none
    st = c.get("/cockpit/tls/status", headers={"X-Cockpit-Nonce": n}).json()
    assert st["self_signed"]["present"] is False
    # generate
    r = c.post("/cockpit/tls/self-signed", headers={"X-Cockpit-Nonce": n})
    assert r.status_code == 200 and r.json()["ok"] is True
    # after: present and parseable
    st2 = c.get("/cockpit/tls/status", headers={"X-Cockpit-Nonce": n}).json()
    assert st2["self_signed"]["present"] is True
    assert "localhost" in st2["self_signed"].get("covers", [])


def test_self_signed_cert_is_valid_x509(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)
    from mongoose.self_signed_tls import generate_self_signed, cert_paths
    res = generate_self_signed(["myhost.local"])
    assert res["ok"] is True
    cert_path, key_path = cert_paths()
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    names = [g.value for g in san if hasattr(g, "value")]
    assert "localhost" in names
    assert "myhost.local" in names


# ── service worker push handlers ──────────────────────────────────────────────────────────────

def test_service_worker_has_push_handlers(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    sw = c.get("/sw.js").text
    assert "addEventListener('push'" in sw, "SW must handle push events"
    assert "notificationclick" in sw, "SW must handle notification clicks"
    assert "showNotification" in sw


def test_device_manifest_present(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    import json
    m = json.loads(c.get("/device/manifest.webmanifest").content)
    assert m["start_url"] == "/device/launchpad"
