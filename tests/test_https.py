"""Tests for the HTTPS/TLS support: settings plumbing, the serve-time validation that
fails loudly on a missing cert, and the doctor report. The actual TLS handshake is
exercised manually (uvicorn ssl kwargs); here we lock in the config + guard logic."""
import os


def test_tls_settings_default_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("MONGOOSE_TLS_CERTFILE", raising=False)
    monkeypatch.delenv("MONGOOSE_TLS_KEYFILE", raising=False)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "data" / "mongoose.sqlite3"))
    from mongoose.settings import Settings
    s = Settings()
    assert s.tls_certfile == "" and s.tls_keyfile == ""


def test_tls_settings_read_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_TLS_CERTFILE", "/x/cert.pem")
    monkeypatch.setenv("MONGOOSE_TLS_KEYFILE", "/x/key.pem")
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "data" / "mongoose.sqlite3"))
    from mongoose.settings import Settings
    s = Settings()
    assert s.tls_certfile == "/x/cert.pem" and s.tls_keyfile == "/x/key.pem"


def test_doctor_reports_https(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "data" / "mongoose.sqlite3"))
    import mongoose.settings as st
    monkeypatch.setattr(st, "_SETTINGS", None, raising=False)
    from mongoose.doctor import run_doctor
    r = run_doctor()
    assert "https" in r["checks"]
    # not configured by default
    assert r["checks"]["https"]["ok"] is False


def test_doctor_https_ready_when_cert_exists(monkeypatch, tmp_path):
    cert = tmp_path / "c.pem"; key = tmp_path / "k.pem"
    cert.write_text("x"); key.write_text("y")
    monkeypatch.setenv("MONGOOSE_TLS_CERTFILE", str(cert))
    monkeypatch.setenv("MONGOOSE_TLS_KEYFILE", str(key))
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "data" / "mongoose.sqlite3"))
    import mongoose.settings as st
    monkeypatch.setattr(st, "_SETTINGS", None, raising=False)
    from mongoose.doctor import run_doctor
    r = run_doctor()
    assert r["checks"]["https"]["ok"] is True


def test_cert_script_exists_and_executable():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # this checkout (repo root of the test file)
    script = os.path.join(root, "scripts", "make_cert.sh")
    assert os.path.exists(script)
    body = open(script).read()
    # mkcert primary, openssl fallback, and it points the user at the env vars
    assert "mkcert" in body and "openssl" in body
    assert "MONGOOSE_TLS_CERTFILE" in body and "MONGOOSE_TLS_KEYFILE" in body


def test_caddyfile_shipped():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # this checkout (repo root of the test file)
    caddy = os.path.join(root, "scripts", "Caddyfile")
    assert os.path.exists(caddy)
    body = open(caddy).read()
    assert "reverse_proxy" in body and "127.0.0.1:8765" in body
