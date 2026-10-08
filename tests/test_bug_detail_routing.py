"""'add detail to <bug id>' must append to THAT bug deterministically, never fall through to the model
(which mis-routed 'add more detail to M42' onto R49) and never silently write to a different bug."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")


def test_add_detail_targets_the_named_bug_not_the_previous_one():
    _fresh()
    from mongoose.services.bug_workflow import log_bug, handle_bug_detail_command, _events
    a = log_bug("first bug about pokes")["bug_id"]
    b = log_bug("second bug about search")["bug_id"]
    # add detail explicitly to the FIRST bug even though the SECOND was created most recently
    r = handle_bug_detail_command(f"add more detail to {a} bug: holding poke should fire in 1.8s")
    assert r and r["ok"] and r["bug_id"] == a, r
    a_details = [e for e in _events(a) if e["kind"] == "detail"]
    b_details = [e for e in _events(b) if e["kind"] == "detail"]
    assert any("1.8s" in e["detail"] for e in a_details), "detail did not land on the named bug"
    assert not b_details, "detail leaked onto the other (most-recent) bug"


def test_add_detail_to_unknown_bug_refuses_rather_than_misrouting():
    _fresh()
    from mongoose.services.bug_workflow import log_bug, handle_bug_detail_command, _events
    real = log_bug("a real bug")["bug_id"]
    r = handle_bug_detail_command("add more detail to Z99 bug: something")
    assert r and r["ok"] is False and r["bug_id"] == "", r
    assert "Z99" in r["reply"]
    # the real bug must NOT have received the stray detail
    assert not [e for e in _events(real) if e["kind"] == "detail"]


def test_ordinary_message_is_not_a_bug_detail_command():
    _fresh()
    from mongoose.services.bug_workflow import handle_bug_detail_command
    assert handle_bug_detail_command("what's the weather tomorrow") is None
    assert handle_bug_detail_command("add milk to the shopping list") is None  # no bug id
