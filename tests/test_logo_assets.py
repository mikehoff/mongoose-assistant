"""Logo integration: the pixel-art mongoose is served, wired into every page, pulses from
transparent, renders crisp, and respects reduced-motion , without breaking the terminal chat CSS."""
from __future__ import annotations

import os
import tempfile


def _client():
    os.environ["MONGOOSE_DB_PATH"] = tempfile.mktemp(suffix=".sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = tempfile.mkdtemp()
    from mongoose.db import init_db
    init_db()
    from fastapi.testclient import TestClient
    from mongoose.app import app
    return TestClient(app)


def test_all_logo_variants_serve():
    c = _client()
    for n in ["mongoose-16.png", "mongoose-32.png", "mongoose-48.png", "mongoose-64.png",
              "mongoose-128.png", "mongoose-256.png", "mongoose-full.png"]:
        r = c.get(f"/static/img/{n}")
        assert r.status_code == 200, n
        assert r.headers["content-type"] == "image/png"
        assert len(r.content) > 100


def test_logo_route_blocks_traversal():
    c = _client()
    assert c.get("/static/img/../app.py").status_code in (400, 404)
    assert c.get("/static/img/nonexistent.png").status_code == 404


def test_logo_assets_have_transparency():
    """The processed logo must be truly transparent (the checkerboard was stripped), so the
    pulse-from-transparent effect actually shows through."""
    from pathlib import Path
    from PIL import Image
    import mongoose
    img_dir = Path(mongoose.__file__).resolve().parent / "static" / "img"
    im = Image.open(img_dir / "mongoose-full.png").convert("RGBA")
    alpha = im.getchannel("A")
    lo, hi = alpha.getextrema()
    assert lo == 0, "logo has no transparent pixels (background not stripped)"
    assert hi == 255, "logo has no opaque pixels (over-stripped)"


def test_logo_wired_into_every_page():
    from mongoose import cockpit as C
    import inspect
    for name, fn in inspect.getmembers(C, inspect.isfunction):
        if name.endswith("_html"):
            h = fn()
            assert "/static/img/mongoose" in h, f"{name} missing logo"
            assert 'rel="icon"' in h, f"{name} missing favicon"


def test_pulse_from_transparent_and_crisp_rendering():
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert "@keyframes logoBreath" in h           # the pulse-from-transparent heartbeat
    assert "@keyframes logoEmerge" in h           # the materialise-from-transparent entrance
    # The brand logo declares an intentional image-rendering mode (crisp-edges) rather than leaving
    # scaling to the browser default. The sillygoose art was deliberately switched away from
    # 'pixelated' (which looked low-quality scaled up) to smooth rendering, so we assert that an
    # explicit rendering intent is present, not the specific removed 'pixelated' value.
    assert "image-rendering:crisp-edges" in h
    assert 'id="brandLogo"' in h


def test_logo_respects_reduced_motion():
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert ".logo-pulse, .logo-emerge{ animation:none" in h


def test_logo_did_not_break_chat_lime_css():
    """Regression guard: the recurring failure mode is the terminal CSS getting trapped/removed."""
    import re
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert "#39ff14" in h and ".irc-win{" in h
    style = next(s for s in re.findall(r"<style>(.*?)</style>", h, re.S) if ".irc-win{" in s)
    irc = style.index(".irc-win{")
    for m in re.finditer(r"@media\s*\(prefers-reduced-motion[^{]*\{", style):
        depth, i, end = 0, m.end() - 1, None
        while i < len(style):
            if style[i] == "{":
                depth += 1
            elif style[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
            i += 1
        assert not (end and m.start() < irc < end), "chat CSS trapped in reduced-motion block"
