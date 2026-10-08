"""The pieces behind HTTPS-on-by-default and font runtime-retry.

- serve auto-provisions a self-signed cert when none is configured; verify the cert it would create
  round-trips (generate -> present/not-expired -> cert files exist).
- the Rajdhani CSS route must re-attempt provisioning every time the cache is still empty (runtime
  retry), and never block the first paint.
"""
from pathlib import Path


def test_self_signed_cert_roundtrip_for_default_https():
    from mongoose import self_signed_tls as T
    gen = T.generate_self_signed()
    assert not (isinstance(gen, dict) and gen.get("ok") is False), gen
    st = T.self_signed_status()
    assert st.get("present") is True
    assert not st.get("expired")
    cert, key = T.cert_paths()
    assert Path(cert).exists() and Path(key).exists()


def test_rajdhani_css_returns_fast_local_fallback_when_uncached(tmp_path, monkeypatch):
    from mongoose import font_assets as F
    monkeypatch.setattr(F, "_cache_root", lambda: tmp_path / "rajdhani")
    monkeypatch.setattr(F, "kick_rajdhani_provisioning", lambda **k: {"status": "kicked"})
    assert F.cached_rajdhani_css() is None
    css, info = F.rajdhani_css(kick=True)
    assert info["ok"] is False and info["source"] == "fallback"
    assert "Rajdhani" in css  # local() fallback keeps the right family before download lands


def test_rajdhani_css_re_kicks_every_time_cache_is_empty(tmp_path, monkeypatch):
    """Runtime retry: each request while the cache is empty re-attempts provisioning, so a download
    that failed at setup (e.g. offline) is retried later rather than left empty forever."""
    from mongoose import font_assets as F
    monkeypatch.setattr(F, "_cache_root", lambda: tmp_path / "rajdhani")
    calls = {"n": 0}
    monkeypatch.setattr(F, "kick_rajdhani_provisioning",
                        lambda **k: (calls.__setitem__("n", calls["n"] + 1), {"status": "kicked"})[1])
    F.rajdhani_css(kick=True)
    F.rajdhani_css(kick=True)
    F.rajdhani_css(kick=True)
    assert calls["n"] == 3


def test_rajdhani_css_serves_cache_without_kicking_once_provisioned(tmp_path, monkeypatch):
    from mongoose import font_assets as F
    monkeypatch.setattr(F, "_cache_root", lambda: tmp_path / "rajdhani")
    monkeypatch.setattr(F, "cached_rajdhani_css", lambda: "/* cached css */")
    kicked = {"n": 0}
    monkeypatch.setattr(F, "kick_rajdhani_provisioning",
                        lambda **k: (kicked.__setitem__("n", kicked["n"] + 1), {})[1])
    css, info = F.rajdhani_css(kick=True)
    assert info["source"] == "cache" and kicked["n"] == 0
