"""Chat and the Tasks screen must answer "is this RTM?" the same way.

Chat carried its own regex whose `\\bleaseholder\\b` could not match the plural, so "remind leaseholders
to inspect their balconies" was not RTM in chat while the task module said it was. Asked in chat, the
owner was told he had 3 RTM tasks; the truth was 6. Two classifiers means the answer depends on which
surface you happen to ask.
"""
import pytest

from mongoose.services import calendar_tasks as CT
from mongoose.services import chat_actions as CA


REAL_RTM = [
    "remind leaseholders to inspect their balconies",   # plural defeated the old word boundary
    "confirmation statement by 31 july",                # statutory filing, absent from the old regex
    "book FRA",                                         # fire risk assessment, an acronym
    "E&M | Payment for Flat 1, 1 Example Road EX1 1AA",
]
NOT_RTM = ["change claim handling details with Insureco", "rics", "belly"]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    from mongoose.db import init_db
    init_db()
    return True


def _add(title, list_name="Tasks"):
    return CT.add_task(CT.TaskIn(source="mongoose", list_name=list_name, title=title,
                                 status="open"))["id"]


def test_chat_module_declares_no_rtm_regex_of_its_own():
    """The structural guarantee: there is only one place to change the vocabulary."""
    import inspect
    src = inspect.getsource(CA)
    assert "_RTM_HINT" not in src, "chat must not keep a private RTM classifier"


def test_the_plural_that_broke_it(store):
    assert CT.guess_is_rtm("remind leaseholders to inspect their balconies") is True


@pytest.mark.parametrize("title", REAL_RTM)
def test_real_rtm_work_is_recognised(store, title):
    assert CT.rtm_verdict({"title": title, "is_rtm": None, "list_name": "Tasks"})[0] is True


@pytest.mark.parametrize("title", NOT_RTM)
def test_unrelated_work_is_not_swept_in(store, title):
    assert CT.rtm_verdict({"title": title, "is_rtm": None, "list_name": "Tasks"})[0] is False


def test_owner_checkbox_beats_the_guess(store):
    """A guess must never override a decision the owner has actually made."""
    row = {"title": "rtm arrears chase", "is_rtm": 0, "list_name": "RTM"}
    is_rtm, basis = CT.rtm_verdict(row)
    assert is_rtm is False and basis == "owner"


def test_basis_is_reported_so_a_guess_can_be_shown_as_a_guess(store):
    assert CT.rtm_verdict({"title": "book FRA", "is_rtm": None, "list_name": "Tasks"})[1] == "guess"
    assert CT.rtm_verdict({"title": "anything", "is_rtm": None, "list_name": "RTM"})[1] == "list"


def test_rtm_scope_is_not_empty_when_nothing_has_been_ticked(store):
    """The live failure: 72 tasks, all is_rtm NULL, and scope='rtm' returned zero rows."""
    for t in REAL_RTM + NOT_RTM:
        _add(t)
    got = CT.list_tasks(limit=100, scope="rtm")
    assert len(got) == len(REAL_RTM), f"expected the guessed RTM work, got {[t['title'] for t in got]}"


def test_chat_reply_and_ui_scope_agree(store):
    for t in REAL_RTM + NOT_RTM:
        _add(t)
    reply = CA.list_tasks_reply("what are the rtm tasks specifically")
    assert reply.startswith(f"{len(REAL_RTM)} open RTM task")
    for t in REAL_RTM:
        assert t in reply
    for t in NOT_RTM:
        assert t not in reply


def test_counts_stay_mutually_exclusive(store):
    """rtm/personal/unclassified must partition the list; effective RTM is reported separately."""
    a, b = _add("rtm arrears chase"), _add("book pianist")
    _add("nin")
    CT.update_task(a, is_rtm=True)
    CT.update_task(b, is_rtm=False)
    n = CT.task_counts()
    assert n["rtm"] + n["personal"] + n["unclassified"] == n["total"]
    assert n["unclassified"] == 1, "an undecided task must not be filed as personal"
    assert "rtm_effective" in n


def test_a_nudge_is_not_reported_as_a_deadline(store):
    """reminder_at is when the owner asked to be poked; printing it as "due" invents a deadline."""
    tid = _add("confirmation statement by 31 july")
    CT.update_task(tid, reminder_at="2026-07-24T09:00:00+00:00")
    reply = CA.list_tasks_reply("what are my outstanding tasks")
    assert "nudge" in reply and "due 2026-07-24" not in reply


def test_client_counterparties_come_from_config_not_source(store, tmp_path, monkeypatch):
    """Insureco is this company's block insurer. That is a fact about one client, so it must be
    configurable: hardcoding a real supplier would reintroduce client data into a source tree being
    sterilised for a public repo, and would force a code change on every change of insurer."""
    import mongoose.services.calendar_tasks as M
    monkeypatch.setattr(M, "_configured_rtm_keywords", lambda: ("insureco",))
    assert M.guess_is_rtm("change claim handling details with Insureco") is True
    assert M.guess_is_rtm("change claim handling details with Hastings") is False


def test_no_client_supplier_names_are_hardcoded():
    """The regression guard for the sterilisation work: the guess vocabulary must stay generic."""
    import inspect
    src = inspect.getsource(CT.guess_is_rtm)
    body = src.split('"""')[-1]          # skip the docstring, which may cite examples
    for name in ("insureco", "harbour", "liftinspect"):
        assert name not in body.lower(), f"{name} is client data and belongs in property.json"


def test_a_broken_config_degrades_instead_of_breaking_the_task_list(store, monkeypatch):
    import mongoose.services.calendar_tasks as M
    monkeypatch.setattr(M, "load_property", None, raising=False)
    assert M.guess_is_rtm("service charge demand") is True, "built-ins must survive a bad config"


def test_blank_keywords_cannot_match_everything(store, monkeypatch, tmp_path):
    """A stray empty string in the config would classify every task as RTM."""
    import mongoose.services.calendar_tasks as M
    from mongoose.services import rtm_example_court as P
    monkeypatch.setattr(P, "load_property", lambda: {"rtm_keywords": ["", " ", "a"]})
    assert M._configured_rtm_keywords() == (), "blank or single-character keywords must be dropped"
    assert M.guess_is_rtm("belly") is False
