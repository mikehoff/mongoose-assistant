"""Regression tests for two real first-run bugs:
1. The Google integration conflated Calendar/Tasks with Gemini under ONE configured badge, so a
   Gemini key (or a default file path) falsely marked the whole thing , and Calendar , 'configured'.
   They're now separate integrations with independent, honest configured semantics.
2. Re-installing showed a PREVIOUS install's progress as already finished, because the progress file
   wasn't reset before the server could serve the page. The start scripts now reset it up front.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_google_env():
    import mongoose.setup_wizard as SW
    saved = {k: os.environ.get(k) for k in ("GOOGLE_API_KEY", "GOOGLE_CLIENT_SECRETS_PATH")}
    saved_path = SW.ENV_PATH
    for k in saved:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        SW.ENV_PATH = saved_path
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    # _default_secret_path()'s legacy-migration copies ./secrets/<file> (relative to CWD, which
    # several tests here os.chdir() into a tempdir) into the STABLE secrets path under the REAL
    # home directory. Without isolating HOME too, this test suite can leak throwaway test OAuth
    # credentials into a real developer's own ~/.mongoose/secrets/.
    os.environ["HOME"] = d
    from mongoose.db import init_db
    init_db()
    return d


def test_gemini_key_does_not_mark_calendar_configured():
    d = _fresh()
    import mongoose.setup_wizard as SW
    envf = Path(d) / ".env"
    SW.ENV_PATH = envf
    envf.write_text("GOOGLE_API_KEY=some-gemini-key\n")
    st = {i["slug"]: i for i in SW.integration_status()}
    # the two are now separate
    assert "google" not in st, "old conflated 'google' slug should be gone"
    assert st["gemini"]["configured"] is True
    assert st["google_calendar"]["configured"] is False


def test_calendar_not_configured_by_default_path_alone():
    d = _fresh()
    import mongoose.setup_wizard as SW
    envf = Path(d) / ".env"
    SW.ENV_PATH = envf
    envf.write_text("")  # nothing set
    st = {i["slug"]: i for i in SW.integration_status()}
    # a default path that doesn't point at a real file must NOT count as configured
    assert st["google_calendar"]["configured"] is False
    assert st["gemini"]["configured"] is False


def test_calendar_configured_only_with_real_oauth_file():
    """CORRECTED truth model: a Google OAuth client file alone is NOT 'configured' , that was the
    false-'configured' bug. It reads needs_signin until an actual token exists; only then configured."""
    d = _fresh()
    import os
    import mongoose.setup_wizard as SW
    envf = Path(d) / ".env"
    SW.ENV_PATH = envf
    os.chdir(d)
    # create a real client file and point at it , this is scaffolding, NOT sign-in
    os.makedirs("secrets", exist_ok=True)
    real = Path(d) / "secrets" / "google-client-secrets.json"
    real.write_text('{"installed":{"client_id":"x"}}')
    envf.write_text(f"GOOGLE_CLIENT_SECRETS_PATH={real}\n")
    st = {i["slug"]: i for i in SW.integration_status()}
    assert st["google_calendar"]["configured"] is False        # NOT configured on the file alone
    assert st["google_calendar"]["auth_state"] == "needs_signin"
    # now add a token (simulating completed sign-in) , NOW it's configured/signed in
    (Path(d) / "secrets" / "google-token.json").write_text(
        '{"token": "ya29.' + "a" * 200 + '", "refresh_token": "1//xyz' + "z" * 80 + '"}')
    st2 = {i["slug"]: i for i in SW.integration_status()}
    assert st2["google_calendar"]["configured"] is True
    assert st2["google_calendar"]["auth_state"] == "signed_in"
    # gemini still independent
    assert st2["gemini"]["configured"] is False


def test_gemini_in_cloud_stage_not_data_stage():
    """Gemini is a cloud model, not a data source, so it belongs in the cloud stage and must NOT be
    counted among 'what Mongoose can see and search'."""
    from mongoose.wizard import WIZARD_STAGES
    stages = {s["key"]: s for s in WIZARD_STAGES}
    assert "google_calendar" in stages["data"]["slugs"]
    assert "gemini" not in stages["data"]["slugs"]
    assert "gemini" in stages["remote"]["slugs"]


def test_install_progress_missing_file_is_not_finished():
    """A missing progress file (e.g. just reset for a new install) must read as pending, never as a
    finished previous install."""
    _fresh()
    from mongoose.install_progress import install_progress
    p = install_progress()
    assert p.get("ready") is False
    assert p.get("progress_pct") == 0
    assert all(s["status"] == "pending" for s in p.get("steps", []))


def test_start_scripts_reset_install_progress_on_first_run():
    """Both the installer and bootstrap must clear a previous install's progress file up front, so
    the live page can't show a prior install as already finished."""
    root = Path(__file__).resolve().parent.parent
    for name in ("Install Mongoose.command", "scripts/bootstrap_macos.sh"):
        text = (root / name).read_text()
        assert "rm -f data/install_progress.json" in text, f"{name} doesn't reset install progress"
