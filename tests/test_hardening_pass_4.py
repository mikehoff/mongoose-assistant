"""Fourth self-directed hardening pass: found by scrutinising the ten audit-addendum fixes
just applied, the same way pass 3 scrutinised passes 1 and 2. (1) the owner-pairing fix
closed the bypass but the secret's entropy (33^6, sized for a human typing a code shown
once) assumed no unthrottled automated guessing; added the same rate-limit primitive built
for poke/mailbox, keyed by caller IP since there is no paired-device identity at that
pre-auth stage. (2) the Polish lounge-trigger lexicon's court/legal stem, written to accept
the word with or without its diacritic, accidentally also matched the common English word
'sad' -- fixed by requiring the diacritic for that one entry (the rest of the legal/debt
category still covers Polish court content without it). (3) the crisis guard closed
reflect() but the audit only tested that one function; focus_step's OLD fallback was worse
than generic, it echoed the crisis text verbatim into a task-breakdown template. Closed
across all three guest text-entry points, not just the one tested."""
from __future__ import annotations

import asyncio
import warnings

warnings.filterwarnings("ignore")


def test_owner_secret_guessing_is_throttled(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    from mongoose.install_progress import begin_install, set_step
    begin_install()
    for k in ["prerequisites", "environment", "database", "server", "services",
             "models", "smoke", "watchdog", "ready"]:
        set_step(k, "done", "audit")
    from mongoose.app import app
    from fastapi.testclient import TestClient
    remote = TestClient(app, client=("192.168.1.99", 52344))
    codes = [remote.post("/install/pair-code", headers={"X-Owner-Secret": f"WRONG{i}"}).status_code
            for i in range(5)]
    assert codes[0] == 401
    assert 429 in codes[1:], "no throttling kicked in during a guessing loop"


def test_owner_secret_legitimate_paths_unaffected_by_the_throttle(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    from mongoose.install_progress import begin_install, set_step
    begin_install()
    for k in ["prerequisites", "environment", "database", "server", "services",
             "models", "smoke", "watchdog", "ready"]:
        set_step(k, "done", "audit")
    from mongoose.app import app
    from fastapi.testclient import TestClient
    loopback = TestClient(app)
    assert loopback.post("/install/pair-code").status_code == 200
    from mongoose.owner_pairing import get_or_create_owner_secret
    secret = get_or_create_owner_secret()
    remote = TestClient(app, client=("192.168.1.60", 9999))
    assert remote.post("/install/pair-code", headers={"X-Owner-Secret": secret}).status_code == 200


def test_lounge_pl_lexicon_no_longer_collides_with_the_english_word_sad(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_LOUNGE_QUIET_MINUTES", "0")
    monkeypatch.setenv("MONGOOSE_LOUNGE_STARTER_COOLDOWN_MINUTES", "0")
    from mongoose.services import lounge_surpriser as LS
    LS.store_human_message(device_id="e1", sender="Alex", text="aww that's such a sad movie ending",
                           line="Alex: aww that's such a sad movie ending")
    assert LS.lounge_snapshot()["safe_to_reflect"] is True


def test_lounge_pl_lexicon_still_catches_court_with_the_diacritic(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_LOUNGE_QUIET_MINUTES", "0")
    monkeypatch.setenv("MONGOOSE_LOUNGE_STARTER_COOLDOWN_MINUTES", "0")
    from mongoose.services import lounge_surpriser as LS
    LS.store_human_message(device_id="e2", sender="Bartek", text="idę do sądu w tej sprawie",
                           line="Bartek: idę do sądu w tej sprawie")
    assert LS.lounge_snapshot()["safe_to_reflect"] is False


def test_crisis_guard_closed_across_all_guest_text_entry_points():
    from mongoose.services import guest_tasks as GT
    for fn, arg in ((GT.focus_step, "task"), (GT.quick_research, "question")):
        r = asyncio.run(fn("guest:abc", "I want to kill myself"))
        assert r.get("crisis") is True
        assert "kill myself" not in r["text"], f"{fn.__name__} echoed crisis text back"
    ordinary = asyncio.run(GT.focus_step("guest:abc", "clean the kitchen"))
    assert not ordinary.get("crisis")
