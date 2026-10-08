"""UX refresh (designer handoff) , locks in the additive cockpit changes so they don't regress.

Covers: 0001 light/dark theme toggle, 0002 brief-led hero, 0003 status strip + button affordances,
0004 command-palette search, 0005 registry-driven setup caption, 0006 welcome state cards, plus the
light-skin overrides for the previously hard-coded dark substrates.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
import mongoose.app as A

client = TestClient(A.app)


def _ui():
    return client.get("/ui").text


def test_theme_toggle_present_and_persisted():
    body = _ui()
    # 0001: the sun/moon control, the light-skin vars, and a persisted, OS-default-aware init.
    assert 'id="themeToggle"' in body
    assert ':root[data-theme="light"]' in body
    assert "mongoose.cockpit.theme" in body              # localStorage key (persistence)
    assert "prefers-color-scheme" in body                # OS default on first load


def test_chat_stays_dark_in_light_skin():
    body = _ui()
    # 0007: the ICQ console defines its own phosphor palette, independent of the theme vars.
    assert "--term-bg:#020a02" in body
    # and it is NOT switched to vars by a light override (intentional)
    assert ":root[data-theme=\"light\"] #chat" not in body


def test_overview_leads_with_brief_hero():
    body = _ui()
    # 0002: the hero + the highlight class for time-sensitive spans.
    assert 'class="hero"' in body
    assert "cross-domain brief" in body
    assert ".brieftext .hl" in body or 'class="hl"' in body


def test_status_strip_and_button_affordances():
    body = _ui()
    # 0003: the at-a-glance strip and the universal button class with hover/active.
    assert 'class="statrip"' in body
    assert ".btn:hover" in body and ".btn:active" in body
    assert "needsrow" in body


def test_command_palette_search():
    body = _ui()
    # 0004: filter chips, result-type badges, keyboard footer, and the global Ctrl/Cmd-K.
    assert 'id="searchFilters"' in body
    assert ".restype" in body and ".t-file" in body
    assert 'id="searchFoot"' in body
    assert "metaKey" in body and "ctrlKey" in body       # global open handler


def test_registry_driven_setup_caption():
    body = _ui()
    # 0005: the self-evident caption that fields/sign-in come from the connector registry.
    assert "connector registry" in body
    assert "regnote" in body


def test_light_skin_overrides_hardcoded_substrates():
    body = _ui()
    # designer follow-up: code/email/token substrates get a light-mode override so Hearth is coherent.
    assert ':root[data-theme="light"] pre{' in body
    assert ':root[data-theme="light"] .email-body{' in body


def test_theme_color_meta_synced_in_init():
    body = _ui()
    # the mobile status-bar colour is updated alongside the skin.
    assert 'meta[name="theme-color"]' in body


def test_system_tab_shows_version_and_build():
    """The System tab must show the running Mongoose version/build (regression: it didn't)."""
    from fastapi.testclient import TestClient
    import mongoose.app as A
    c = TestClient(A.app)
    sm = c.get("/cockpit/system-map").json()
    assert sm.get("version"), "system-map must expose version"
    assert "release" in sm
    from mongoose.cockpit import cockpit_html
    body = cockpit_html()
    assert "m.release" in body and "running build on this machine" in body
