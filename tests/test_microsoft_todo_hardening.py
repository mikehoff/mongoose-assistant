"""Tests for the hardened Microsoft To Do integration: non-interactive token for background syncs
(never hangs on device-code), Graph pagination (large lists not silently truncated), per-list
resilience (one bad list doesn't abort the sync), auth-vs-transient error distinction, completed-task
filtering, and the background sync job."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest.mock as M


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    from mongoose.db import init_db
    init_db()
    return d


class _Resp:
    def __init__(self, body, status=200):
        self._b = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._b


def _client_factory(get_fn):
    class _C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, params=None):
            return get_fn(url, headers, params)
    return _C


def test_no_token_non_interactive_returns_auth_error_not_hang():
    """The scheduler calls this with allow_interactive=False. With no token it must return a
    structured auth error, NOT launch a device-code flow (which would hang forever)."""
    _fresh()
    import mongoose.connectors.microsoft_graph as mg
    with M.patch.object(mg, "_headers", side_effect=mg.MicrosoftAuthError("not signed in")):
        r = mg.sync_microsoft_todo(allow_interactive=False)
    assert r["ok"] is False
    assert r["error"] == "auth"
    assert r["needs_signin"] is True


def test_pagination_follows_nextlink():
    """More lists than one page returns must all be synced (nextLink followed)."""
    _fresh()
    import mongoose.connectors.microsoft_graph as mg

    def get_fn(url, headers=None, params=None):
        if "/todo/lists" in url and "/tasks" not in url:
            if "skiptoken" in url:
                return _Resp({"value": [{"id": "L2", "displayName": "B"}]})
            return _Resp({"value": [{"id": "L1", "displayName": "A"}],
                          "@odata.nextLink": mg.GRAPH + "/me/todo/lists?$skiptoken=X"})
        if "/tasks" in url:
            lid = "L1" if "/L1/" in url else "L2"
            return _Resp({"value": [{"id": "T_" + lid, "title": "t", "status": "open"}]})
        return _Resp({"value": []})

    with M.patch.object(mg, "_headers", return_value={"Authorization": "Bearer x"}), \
         M.patch.object(mg.httpx, "Client", _client_factory(get_fn)):
        r = mg.sync_microsoft_todo(allow_interactive=False)
    assert r["ok"] is True
    assert r["count"] == 2, f"both lists' tasks should sync via pagination, got {r}"


def test_completed_tasks_filtered_by_default():
    _fresh()
    import mongoose.connectors.microsoft_graph as mg

    def get_fn(url, headers=None, params=None):
        if "/todo/lists" in url and "/tasks" not in url:
            return _Resp({"value": [{"id": "L1", "displayName": "A"}]})
        if "/tasks" in url:
            return _Resp({"value": [
                {"id": "T1", "title": "open", "status": "open"},
                {"id": "T2", "title": "done", "status": "completed"},
            ]})
        return _Resp({"value": []})

    with M.patch.object(mg, "_headers", return_value={"Authorization": "Bearer x"}), \
         M.patch.object(mg.httpx, "Client", _client_factory(get_fn)):
        r = mg.sync_microsoft_todo(allow_interactive=False)
    assert r["count"] == 1
    assert r["skipped_completed"] == 1
    # opt-in to completed
    with M.patch.object(mg, "_headers", return_value={"Authorization": "Bearer x"}), \
         M.patch.object(mg.httpx, "Client", _client_factory(get_fn)):
        r2 = mg.sync_microsoft_todo(allow_interactive=False, include_completed=True)
    assert r2["count"] == 2


def test_one_failing_list_does_not_abort_sync():
    """A deleted/forbidden list must be a warning, not a total failure , the other lists still sync."""
    _fresh()
    import mongoose.connectors.microsoft_graph as mg

    def get_fn(url, headers=None, params=None):
        if "/todo/lists" in url and "/tasks" not in url:
            return _Resp({"value": [{"id": "GOOD", "displayName": "G"},
                                    {"id": "BAD", "displayName": "B"}]})
        if "/tasks" in url and "BAD" in url:
            return _Resp({}, status=500)
        if "/tasks" in url and "GOOD" in url:
            return _Resp({"value": [{"id": "T", "title": "survives", "status": "open"}]})
        return _Resp({"value": []})

    with M.patch.object(mg, "_headers", return_value={"Authorization": "Bearer x"}), \
         M.patch.object(mg.httpx, "Client", _client_factory(get_fn)):
        r = mg.sync_microsoft_todo(allow_interactive=False)
    assert r["ok"] is True
    assert r["count"] == 1, "the good list should still sync"
    assert r.get("warnings"), "the bad list should produce a warning"


def test_401_is_auth_error():
    """A 401 mid-sync means the token died , report as auth (re-sign-in), not transient."""
    _fresh()
    import mongoose.connectors.microsoft_graph as mg

    def get_fn(url, headers=None, params=None):
        return _Resp({}, status=401)

    with M.patch.object(mg, "_headers", return_value={"Authorization": "Bearer x"}), \
         M.patch.object(mg.httpx, "Client", _client_factory(get_fn)):
        r = mg.sync_microsoft_todo(allow_interactive=False)
    assert r["ok"] is False
    assert r["error"] == "auth"


def test_token_non_interactive_raises_not_blocks():
    """_token(allow_interactive=False) must raise MicrosoftAuthError rather than call
    initiate_device_flow when there's no account."""
    _fresh()
    os.environ["MS_CLIENT_ID"] = "test-client"
    import mongoose.connectors.microsoft_graph as mg

    fake_app = M.MagicMock()
    fake_app.get_accounts.return_value = []
    # if device flow were called, fail loudly
    fake_app.initiate_device_flow.side_effect = AssertionError("must NOT start device flow when non-interactive")
    with M.patch.object(mg, "_app_and_cache", return_value=(fake_app, M.MagicMock())):
        try:
            mg._token(allow_interactive=False)
            assert False, "should have raised MicrosoftAuthError"
        except mg.MicrosoftAuthError:
            pass
    os.environ.pop("MS_CLIENT_ID", None)


# ---- background sync job ----

def test_sync_tasks_job_registered_and_skips_cleanly():
    _fresh()
    from mongoose.services.scheduler import _build_registry, _job_sync_tasks
    assert "sync_tasks" in _build_registry()
    # with no Microsoft config it skips cleanly (doesn't error or block)
    r = asyncio.run(_job_sync_tasks())
    assert r.get("skipped") == "microsoft_not_configured"


def test_tasks_sync_minutes_setting():
    _fresh()
    from mongoose.settings import get_settings
    assert isinstance(get_settings().tasks_sync_minutes, int)
