"""Regression coverage for the install browser handoff.

The terminal may spend minutes on Homebrew/Python/pip before the FastAPI app can serve /install.
The human must still see a real browser page immediately, not a blank spinner on the future /install
URL. The preloader is a stdlib loopback page on a temporary port that hands off only after /install
is proven renderable.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "Install Mongoose.command"
BOOTSTRAP = ROOT / "scripts" / "bootstrap_macos.sh"
PRELOADER = ROOT / "scripts" / "install_preloader.py"


def test_installer_ships_stdlib_preloader_script():
    text = PRELOADER.read_text()
    assert "ThreadingHTTPServer" in text
    assert "https://127.0.0.1" in text and "https://localhost" in text
    assert "/install" in text and "/status" in text
    assert "from fastapi" not in text.lower() and "import fastapi" not in text.lower(), \
        "preloader must not require the app stack it is waiting for"


def test_wrapper_opens_preloader_before_bootstrap_not_future_install_url():
    text = WRAPPER.read_text()
    first_run = text.split("Starting the first-run bootstrap", 1)[1].split("if bash scripts/bootstrap_macos.sh", 1)[0]
    assert "launch_install_preloader" in first_run
    assert "install_preloader.py" in text
    assert "MONGOOSE_INSTALL_PRELOADER=1" in text
    assert "http://127.0.0.1:${INSTALL_PRELOADER_PORT}/installing" in text
    assert "open_install_screen" not in first_run or "if ! launch_install_preloader" in first_run


def test_existing_install_restart_also_gets_preloader():
    text = WRAPPER.read_text()
    existing = text.split("Existing .venv found. Checking Ollama and restarting Mongoose", 1)[1]
    existing = existing.split("started=0", 1)[0]
    assert "launch_install_preloader \"Mongoose is restarting\"" in existing


def test_bootstrap_does_not_open_second_install_tab_when_preloader_is_active():
    text = BOOTSTRAP.read_text()
    assert "MONGOOSE_INSTALL_PRELOADER" in text
    section = text.split("Open the live /install page NOW", 1)[1].split("# Voice mic toolchain", 1)[0]
    assert "mongoose_open_local \"/install\"" in section
    assert "The temporary browser progress page is open" in section
    assert "else" in section, "bootstrap should only open /install directly when no preloader is watching"
