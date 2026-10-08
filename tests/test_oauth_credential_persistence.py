"""Field bugs, both closed:
1. OAuth credential files (Google token, Microsoft MSAL cache) defaulted to ./secrets/<file>,
   relative to whatever folder the installer happened to run from. Every fresh unzip -- which is
   what an update actually is in practice, not a strict in-place update.sh run -- silently lost
   all sign-in state. Fixed with a stable ~/.mongoose/secrets/ default plus one-time migration,
   mirroring the already-established _default_db_path pattern exactly.
2. Two independently-implemented "is signed in" checks (one path-complete with legacy fallbacks,
   one single-path with none) could disagree on the exact same fact, producing two cards on the
   same page with contradictory Microsoft sign-in state. Fixed with a single source of truth
   every consumer now calls."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def test_default_secret_paths_are_stable_not_relative():
    from mongoose.settings import _default_secret_path
    import unittest.mock as mock
    from pathlib import Path
    fake_home = Path("/tmp/_stable_secret_test_home")
    with mock.patch("os.path.expanduser", side_effect=lambda p: str(fake_home) if p == "~" else p):
        p = _default_secret_path("some-token.json")
    assert p == str(fake_home / ".mongoose" / "secrets" / "some-token.json")
    assert not p.startswith("./secrets")


def test_legacy_relative_token_is_migrated_forward(tmp_path, monkeypatch):
    from mongoose.settings import _default_secret_path
    import unittest.mock as mock
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    workdir = tmp_path / "old_install_folder"
    (workdir / "secrets").mkdir(parents=True)
    (workdir / "secrets" / "msal-token-cache.bin").write_text("real-token-contents")
    monkeypatch.chdir(workdir)
    with mock.patch("os.path.expanduser", side_effect=lambda p: str(fake_home) if p == "~" else p):
        p = _default_secret_path("msal-token-cache.bin")
    stable = fake_home / ".mongoose" / "secrets" / "msal-token-cache.bin"
    assert stable.exists()
    assert stable.read_text() == "real-token-contents"
    assert p == str(stable)


def test_single_source_of_truth_agrees_across_all_consumers(tmp_path, monkeypatch):
    """The exact drift scenario: a token exists at a legacy candidate path, NOT at the settings
    path. Every consumer (oauth, onboarding, setup_wizard) must agree it is signed in."""
    workdir = tmp_path / "drift_workdir"
    (workdir / "secrets").mkdir(parents=True)
    (workdir / "secrets" / "microsoft-token.json").write_text('{"fake":"' + "t" * 120 + '"}')  # realistic size: tiny placeholders now correctly read as NOT signed in
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("MS_TOKEN_CACHE", str(tmp_path / "nowhere" / "msal-token-cache.bin"))

    import mongoose.oauth as O
    import mongoose.onboarding as OB
    import mongoose.setup_wizard as SW

    assert O.microsoft_token_present() is True
    assert "microsoft" in OB.connected_sources()
    assert SW._microsoft_auth_state({}) == "signed_in"


def test_no_token_anywhere_all_consumers_agree(tmp_path, monkeypatch):
    workdir = tmp_path / "empty_workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("MS_TOKEN_CACHE", str(tmp_path / "nowhere2" / "msal-token-cache.bin"))

    import mongoose.oauth as O
    import mongoose.onboarding as OB
    import mongoose.setup_wizard as SW

    assert O.microsoft_token_present() is False
    assert "microsoft" not in OB.connected_sources()
    assert SW._microsoft_auth_state({}) == "needs_signin"


def test_push_notification_card_has_the_button_its_own_blurb_promises():
    from mongoose.setup_wizard import integration_status
    from mongoose.cockpit import cockpit_html
    rows = integration_status()
    push = next(r for r in rows if r["slug"] == "push")
    assert "Click 'Set up native push' below" in push["help"]
    assert "Click 'Set up phone push' below" not in push["help"]
    html = cockpit_html()
    assert 'ig.slug==="push"' in html
    assert "setupNtfyFromCard(this)" in html
    assert ">Set up native push</button>" in html
    assert "async function setupNtfyFromCard(btn)" in html
    # must not collide with the separate dedicated push panel's global element
    assert '.querySelector(".wiz-msg")' in html


def test_placeholder_token_files_do_not_read_as_signed_in(tmp_path):
    """A zero-byte or tiny placeholder token file (interrupted write, touch'd file, bare {})
    must not read as 'signed in': that false positive triggers real Graph/Gmail sync attempts
    that then fail confusingly. Independently reported against 1.26.1 and ported here."""
    from mongoose.oauth import _plausible_token_file
    tiny = tmp_path / "tiny.json"
    tiny.write_text("{}")
    assert _plausible_token_file(tiny) is False
    real = tmp_path / "real.json"
    real.write_text('{"token":"' + "x" * 200 + '"}')
    assert _plausible_token_file(real) is True
    assert _plausible_token_file(tmp_path / "missing.json") is False
