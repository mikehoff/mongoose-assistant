"""Rajdhani font provisioning: multi-source with automatic fallback (Bunny Fonts primary,
Google Fonts secondary), so a single source being unreachable -- including a DNS blocklist
filtering fonts.googleapis.com/fonts.gstatic.com, a real-world case -- no longer surfaces as
a hard failure or a raw exception dumped to the terminal."""
from __future__ import annotations

import shutil
import unittest.mock as mock
import warnings

warnings.filterwarnings("ignore")

import mongoose.font_assets as F

FAKE_CSS = (
    "@font-face{font-family:'Rajdhani';font-weight:500;"
    "src:url(https://EXAMPLE-CDN/a.woff2) format('woff2');}\n"
    "@font-face{font-family:'Rajdhani';font-weight:600;"
    "src:url(https://EXAMPLE-CDN/b.woff2) format('woff2');}\n"
)
FAKE_BYTES = b"FAKEWOFF2DATA"


def _clear_cache():
    shutil.rmtree(F._cache_root(), ignore_errors=True)


def test_two_sources_configured_bunny_first():
    assert len(F.RAJDHANI_CSS_URLS) == 2
    assert "bunny.net" in F.RAJDHANI_CSS_URLS[0]
    assert "googleapis.com" in F.RAJDHANI_CSS_URLS[1]


def test_first_source_blocked_falls_back_to_second(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "_cache_root", lambda: tmp_path / "rajdhani")
    _clear_cache()

    def fake_fetch_text(url, *, timeout):
        if url == F.RAJDHANI_CSS_URLS[0]:
            raise OSError("[Errno -2] Name or service not known")  # DNS-block signature
        return FAKE_CSS.replace("EXAMPLE-CDN", "fonts.gstatic.com")

    with mock.patch.object(F, "_fetch_text", side_effect=fake_fetch_text), \
         mock.patch.object(F, "_fetch_bytes", return_value=FAKE_BYTES):
        r = F.provision_rajdhani(timeout=15.0, force=True)
    assert r["ok"] is True
    assert r["source_css"] == F.RAJDHANI_CSS_URLS[1]
    css = F._css_path().read_text()
    assert "fonts.gstatic.com" not in css
    assert "/static/fonts/provisioned/rajdhani/" in css


def test_both_sources_blocked_fails_cleanly_no_raw_traceback(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "_cache_root", lambda: tmp_path / "rajdhani")
    _clear_cache()
    with mock.patch.object(F, "_fetch_text",
                           side_effect=OSError("[Errno -2] Name or service not known")):
        r = F.provision_rajdhani(timeout=6.0, force=True)
    assert r["ok"] is False
    assert r["sources_tried"] == 2
    assert r["error"] == "OSError"
    assert "Traceback" not in str(r)


def test_bootstrap_reports_result_without_dumping_raw_exceptions():
    t = open("scripts/bootstrap_macos.sh").read()
    start = t.index("Provisioning the Sillygoose display font")
    open_marker = t.index("PYEOF", start)
    close_marker = t.index("PYEOF", open_marker + 5)
    seg = t[start:close_marker]
    assert "sources_tried" in seg
    assert "will retry automatically at runtime" in seg
