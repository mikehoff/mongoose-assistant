"""Public-facing hardening gates: security headers everywhere, first-run surfaces closed to
the internet under MONGOOSE_PUBLIC_FACING, canonical URL resolution, and the chain validator."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient

import mongoose.app as A

INTERNET = ("203.0.113.9", 40000)


def test_security_headers_on_every_response():
    c = TestClient(A.app)
    for path in ("/health", "/definitely-not-a-route"):
        r = c.get(path)
        assert r.headers.get("x-content-type-options") == "nosniff", path
        assert r.headers.get("x-frame-options") == "DENY", path
        assert r.headers.get("referrer-policy") == "no-referrer", path
        assert "strict-transport-security" not in r.headers  # never on plain http


def test_hsts_only_over_tls():
    c = TestClient(A.app)
    r = c.get("/health", headers={"x-forwarded-proto": "https"})
    assert "max-age" in r.headers.get("strict-transport-security", "")


def test_public_facing_closes_first_run_surfaces(monkeypatch):
    monkeypatch.setenv("MONGOOSE_PUBLIC_FACING", "true")
    c = TestClient(A.app, client=INTERNET)
    for path in ("/welcome", "/install", "/tutorial"):
        assert c.get(path).status_code in (401, 403), path
    for path in ("/pair", "/health", "/login"):
        assert c.get(path).status_code == 200, path
    # loopback (the Mac itself) keeps first-run access
    local = TestClient(A.app)
    assert local.get("/welcome").status_code == 200


def test_first_run_surfaces_open_when_not_public_facing(monkeypatch):
    monkeypatch.delenv("MONGOOSE_PUBLIC_FACING", raising=False)
    c = TestClient(A.app, client=INTERNET)
    assert c.get("/welcome").status_code == 200


def test_canonical_url_resolution(monkeypatch):
    from mongoose.services.remote_access import public_base_url, public_hostname
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "yourname.duckdns.org")
    assert public_base_url() == "https://yourname.duckdns.org"
    assert public_hostname() == "yourname.duckdns.org"
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "https://yourname.duckdns.org/")
    assert public_base_url() == "https://yourname.duckdns.org"


def test_remote_readiness_route_gated_and_structured():
    c = TestClient(A.app, client=INTERNET)
    assert c.get("/remote/readiness?probe=false").status_code in (401, 403)
    local = TestClient(A.app)
    r = local.get("/remote/readiness?probe=false",
                  headers={"x-cockpit-nonce": A._COCKPIT_NONCE}).json()
    assert "checks" in r and "failing" in r
    labels = {ch["label"] for ch in r["checks"]}
    assert "Canonical public URL" in labels and "Remote auth posture" in labels


def test_doctor_includes_remote_access(monkeypatch):
    monkeypatch.setenv("MONGOOSE_PUBLIC_URL", "yourname.duckdns.org")
    from mongoose.doctor import run_doctor
    r = run_doctor(timeout_seconds=0.3)
    assert "remote_access" in r["checks"]
