"""Gates for the underlooked always-on failure modes: utcnow() deprecation rot, TLS cert
expiry, unbounded log-table growth, and the doctor's new machine-health checks."""
from __future__ import annotations

import datetime
import pathlib
import warnings

warnings.filterwarnings("ignore")

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "mongoose"


def test_no_naive_utcnow_outside_rtm():
    offenders = []
    for f in SRC.rglob("*.py"):
        if f.name.startswith("rtm_"):
            continue
        body = f.read_text()
        if "datetime.utcnow()" in body or "asyncio.get_event_loop()" in body:
            offenders.append(str(f))
    assert not offenders, f"deprecated patterns reintroduced (naive utcnow / get_event_loop): {offenders}"


def test_tls_renewal_skips_when_healthy_and_renews_near_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    import importlib

    import mongoose.self_signed_tls as T
    importlib.reload(T)
    assert T.renew_if_expiring()["skipped"].startswith("no self-signed cert")
    assert T.generate_self_signed(extra_hostnames=["mongoose.local"])["ok"]
    healthy = T.renew_if_expiring()
    assert healthy.get("skipped", "").startswith("cert healthy")
    # forge a 5-day cert in place; renewal must fire and keep the extra SAN
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Mongoose Local")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=5))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.DNSName("mongoose.local")]), critical=False)
            .sign(key, hashes.SHA256()))
    cp, kp = T.cert_paths()
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.TraditionalOpenSSL,
                                     serialization.NoEncryption()))
    r = T.renew_if_expiring()
    assert r.get("renewed") and "mongoose.local" in r["covers"]
    assert (T.days_until_expiry() or 0) > 700


def test_retention_deletes_only_old_rows_and_only_from_the_allowlist():
    from mongoose.db import connect
    from mongoose.services.db_maintenance import (RETENTION_DAYS, apply_retention)
    # the allowlist must never contain user-data or statutory tables
    for forbidden in ("rtm_", "knowledge_records", "knowledge_archive", "whatsapp_messages",
                      "bs_", "wellness", "diary", "memory_facts", "conversations", "song_exchanges"):
        assert not any(tb.startswith(forbidden) or tb == forbidden for tb in RETENTION_DAYS), forbidden
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=999)).isoformat()
    new = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with connect() as con:
        con.execute("INSERT INTO audit_log (module, action, success, created_at) VALUES ('t','t',1,?)", (old,))
        con.execute("INSERT INTO audit_log (module, action, success, created_at) VALUES ('t','t',1,?)", (new,))
        con.commit()
    res = apply_retention()
    assert res["ok"] and res["deleted"].get("audit_log") == 1
    with connect() as con:
        left = con.execute("SELECT created_at FROM audit_log").fetchall()
    assert len(left) == 1 and left[0][0] == new


def test_retention_kill_switch(monkeypatch):
    monkeypatch.setenv("MONGOOSE_RETENTION_DISABLED", "1")
    from mongoose.services.db_maintenance import apply_retention
    assert "MONGOOSE_RETENTION_DISABLED" in apply_retention()["skipped"]


def test_run_maintenance_reports_health():
    from mongoose.services.db_maintenance import run_maintenance
    m = run_maintenance()
    assert m["ok"] and m["health"]["quick_check"] == "ok"


def test_scheduler_registers_maintenance_and_tls_jobs():
    from mongoose.services import scheduler as S
    reg = S._build_registry()
    assert "tls_renewal" in reg and "db_maintenance" in reg
    assert reg["db_maintenance"][0][0] == "daily"
    assert reg["tls_renewal"][0][0] == "weekly"


def test_doctor_reports_machine_health(monkeypatch):
    from mongoose.doctor import run_doctor
    r = run_doctor(timeout_seconds=0.3)
    for key in ("disk_space", "database_health", "tls_certificate", "backup_freshness"):
        assert key in r["checks"], key
        assert "detail" in r["checks"][key]
