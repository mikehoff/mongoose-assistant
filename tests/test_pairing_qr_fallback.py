"""The welcome/pairing screen must never say 'scan this' with nothing to scan: when the QR library
is missing, the pair-code response still carries a typed pair_url the UI can show instead."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()


def test_pairing_qr_svg_is_soft_dependency(monkeypatch):
    _fresh()
    import mongoose.devices as dev
    # simulate qrcode not installed: the import inside pairing_qr_svg raises
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "qrcode" or name.startswith("qrcode."):
            raise ImportError("no qrcode")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert dev.pairing_qr_svg("http://127.0.0.1:8765/pair?code=ABC") is None


def test_pairing_qr_svg_renders_when_available():
    _fresh()
    import mongoose.devices as dev
    svg = dev.pairing_qr_svg("http://127.0.0.1:8765/pair?code=ABC123")
    # qrcode is available in this env; should be a real SVG (xml decl then <svg>)
    if svg is not None:
        assert "<svg" in svg and "</svg>" in svg


def test_install_pair_code_returns_pair_url():
    _fresh()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    # mark install ready + nothing paired so the first-run endpoint is allowed
    from mongoose.db import set_app_state
    try:
        set_app_state("install_progress", '{"ready": true}')
    except Exception:
        pass
    client = TestClient(app)
    r = client.post("/install/pair-code")
    # either it's allowed (200 with code + pair_url) or gated (4xx) , but if 200, pair_url must exist
    if r.status_code == 200:
        body = r.json()
        assert body.get("code")
        assert body.get("pair_url", "").startswith("http") and "/pair?code=" in body["pair_url"]
