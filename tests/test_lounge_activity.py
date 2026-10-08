"""The lounge activity tracker decides when a person is 'messaging totally alone', which is
when Mongoose engages instead of staying the quiet sidekick. A fresh arrival is alone after
just 1 minute of others' silence; an established participant after 4 minutes. Presence is by
recent MESSAGING, not membership, so an idle guest who joined long ago is not company."""
import mongoose.services.lounge_surpriser as LS
from mongoose.db import connect

_MID = [0]


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "la.sqlite3"))
    from mongoose.db import init_db
    init_db()
    with connect() as con:
        LS._ensure_tables(con)


def _meta(device_id, seconds_ago, kind="human"):
    _MID[0] += 1
    ago = f"-{int(seconds_ago)} seconds"
    conv = LS.GROUP_CONVERSATION_ID
    with connect() as con:
        LS._ensure_tables(con)
        con.execute("INSERT OR IGNORE INTO conversations(id, title, created_at, updated_at) "
                    "VALUES (?, 'lounge', datetime('now'), datetime('now'))", (conv,))
        con.execute("INSERT INTO messages(id, conversation_id, role, content, created_at) "
                    "VALUES (?,?,?,?, datetime('now', ?))",
                    (_MID[0], conv, "user", f"{device_id}: hi", ago))
        con.execute("INSERT INTO lounge_message_meta(message_id, device_id, sender, kind, created_at) "
                    "VALUES (?,?,?,?, datetime('now', ?))",
                    (_MID[0], device_id, device_id, kind, ago))
        con.commit()


def test_new_joiner_alone_when_nobody_else_active(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _meta("alice", 5)                       # alice's first line, 5s ago -> fresh arrival
    assert LS.is_messaging_alone("alice") is True


def test_new_joiner_not_alone_if_other_within_one_minute(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _meta("alice", 5)
    _meta("bob", 30)                        # bob 30s ago -> inside alice's 1-min window
    assert LS.is_messaging_alone("alice") is False


def test_new_joiner_alone_if_other_older_than_one_minute(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _meta("alice", 5)
    _meta("bob", 90)                        # bob 90s ago -> outside the 1-min window
    assert LS.is_messaging_alone("alice") is True


def test_established_participant_uses_four_minute_window(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _meta("carol", 1200)                    # first line 20 min ago -> established
    _meta("carol", 5)
    _meta("dave", 180)                      # dave 3 min ago -> inside the 4-min window
    assert LS.is_messaging_alone("carol") is False


def test_established_alone_if_other_older_than_four_minutes(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _meta("carol", 1200)
    _meta("carol", 5)
    _meta("dave", 300)                      # dave 5 min ago -> outside the 4-min window
    assert LS.is_messaging_alone("carol") is True


def test_should_model_reply_engages_when_alone_but_not_with_company(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _meta("solo", 5)                        # solo, fresh, alone
    assert LS.should_model_reply_to(text="oh cooool", sender="solo", device_id="solo") is True
    _meta("mate", 20)                       # mate now active -> solo has company
    assert LS.should_model_reply_to(text="oh cooool", sender="solo", device_id="solo") is False
