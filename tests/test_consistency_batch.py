"""Consistency + reliability meta-patch tests.

Two overarching failure patterns this batch addresses:
  1. "Operation says it started, then UI has no timeout/error path" (DuckDNS save hangs, mail sync
     freezes, search never returns, suggest blocks overview). Now all have timeouts and visible errors.
  2. "Server restart uses wrong cwd" (enable-phone restarts server with relative paths, so data/logs/
     .env resolve to ~/Downloads/ instead of the install dir , causing logs 404, pairing fail, settings
     loss). Now all restarts use absolute paths from _repo_root().
  3. "Subsystem inconsistency: chat says it doesn't have your tasks when it just synced 60"
     (get_tasks / get_calendar added to READ_TOOLS; chat can now query synced task data directly).
  4. "Pairing gate blocks post-enable-phone" (token presence no longer blocks first pairing).
"""
import asyncio
import os
import tempfile
from datetime import datetime


def _fresh(**env):
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_INPROCESS_SCHEDULER"] = "false"
    os.environ["MONGOOSE_RUNTIME_WATCHDOG"] = "false"
    for k, v in env.items():
        os.environ[k] = v
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()


# --- Task tool tests ---

def test_get_tasks_returns_synced_tasks():
    _fresh()
    from mongoose.services.calendar_tasks import TaskIn, add_task
    add_task(TaskIn(source="microsoft", source_task_id="t1", list_name="Work",
                    title="Review RTM documents"))
    from mongoose.tools import _tool_get_tasks
    result = asyncio.run(_tool_get_tasks({}))
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["lists"][0]["name"] == "Work"
    assert result["tasks"][0]["title"] == "Review RTM documents"


def test_get_tasks_filters_by_list():
    _fresh()
    from mongoose.db import connect
    now = datetime.now().isoformat()
    with connect() as c:
        c.executemany(
            "INSERT INTO tasks(source,source_task_id,list_name,title,status,last_seen) VALUES (?,?,?,?,?,?)",
            [("ms","t1","Work","Task A","not_started",now), ("ms","t2","Personal","Task B","not_started",now)],
        )
    from mongoose.tools import _tool_get_tasks
    result = asyncio.run(_tool_get_tasks({"list_name": "Work"}))
    assert len(result["lists"]) == 1
    assert result["lists"][0]["name"] == "Work"
    assert result["count"] == 1  # only the Work task


def test_get_tasks_in_read_tools():
    from mongoose.tools import READ_TOOLS
    assert "get_tasks" in READ_TOOLS
    assert "get_calendar" in READ_TOOLS


def test_system_status_tasks_area():
    _fresh()
    from mongoose.services.status_tools import tool_system_status
    r = asyncio.run(tool_system_status({"area": "tasks"}))
    assert r.get("area") == "tasks"
    assert "tasks" in r


# --- Server restart absolute-path tests ---

def test_enable_phone_restart_uses_absolute_cwd():
    """The restart must pass cwd=_repo_root() so it works regardless of where the CLI was run from."""
    import inspect
    import mongoose.enable_phone as ep
    src = inspect.getsource(ep.enable_phone)
    assert "cwd=str(install_dir)" in src
    assert '_repo_root' in src


def test_cockpit_restart_soon_uses_absolute_cwd():
    """Both _restart_soon functions in app.py must use an absolute cwd (_repo_root()), not a
    relative path that could break if the process's CWD differs from where it was launched."""
    import mongoose.app as A
    src = open(A.__file__).read()
    assert src.count("install_dir = str(_repo_root())") >= 2  # one per _restart_soon
    assert src.count("cwd=install_dir") >= 2  # the actual Popen call, per _restart_soon


# --- Pairing gate: allows pairing after enable-phone (even with a token set) ---

def test_pair_code_allowed_with_token_when_nothing_paired():
    _fresh(MONGOOSE_API_TOKEN="test-token-123", MONGOOSE_HOST="0.0.0.0", MONGOOSE_ALLOW_REMOTE="true")
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    import mongoose.app as A
    from fastapi.testclient import TestClient
    c = TestClient(A.app)
    # should NOT 403 just because a token is set — enable-phone writes a token and still needs to pair
    r = c.post("/install/pair-code", headers={"host": "127.0.0.1"})
    assert r.status_code != 403, f"pair-code 403'd after enable-phone: {r.json()}"


# --- Timeout meta-pattern: suggest and daily have wait_for ---

def test_cockpit_suggest_has_timeout():
    import mongoose.app as A
    src = open(A.__file__).read()
    # find cockpit_suggest and verify wait_for is nearby
    idx = src.index('async def cockpit_suggest')
    region = src[idx:idx+600]
    assert 'wait_for' in region


def test_cockpit_daily_has_timeout():
    import mongoose.app as A
    src = open(A.__file__).read()
    idx = src.index('async def cockpit_daily')
    region = src[idx:idx+600]
    assert 'wait_for' in region


# --- Google OAuth redirect URI is shown before sign-in ---

def test_google_setup_shows_redirect_uri():
    from mongoose.cockpit import COCKPIT_HTML
    assert "localhost:8766/" in COCKPIT_HTML
    # The tile tells the user to register the exact redirect URI (wording updated to surface a
    # proactive port-busy warning and the resolved URI).
    assert "redirect URI" in COCKPIT_HTML
