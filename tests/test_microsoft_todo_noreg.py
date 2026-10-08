"""Microsoft To Do works WITHOUT app registration: the connector falls back to the well-known public
client and the 'consumers' authority for personal accounts, requests only Tasks.Read, and fails cleanly
when offline."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    for k in ("MS_CLIENT_ID", "MONGOOSE_MS_CLIENT_ID"):
        os.environ.pop(k, None)
    from mongoose.db import init_db
    init_db()


def test_falls_back_to_well_known_public_client_when_unregistered():
    _fresh()
    from mongoose.connectors.microsoft_graph import _effective_client_id, WELL_KNOWN_PUBLIC_CLIENT_ID
    assert _effective_client_id() == WELL_KNOWN_PUBLIC_CLIENT_ID


def test_personal_account_uses_consumers_authority():
    _fresh()
    from mongoose.connectors.microsoft_graph import _effective_authority
    assert _effective_authority().endswith("/consumers")


def test_todo_only_scopes_are_minimal():
    from mongoose.connectors.microsoft_graph import TODO_ONLY_SCOPES
    assert "Tasks.Read" in TODO_ONLY_SCOPES
    assert "Mail.Read" not in TODO_ONLY_SCOPES
    assert "Calendars.Read" not in TODO_ONLY_SCOPES


def test_registered_client_uses_common_or_tenant():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MS_CLIENT_ID"] = "00000000-1111-2222-3333-444444444444"
    try:
        import mongoose.settings as S
        if hasattr(S, "_settings"):
            S._settings = None
        if hasattr(S.get_settings, "cache_clear"):
            S.get_settings.cache_clear()
        from mongoose.db import init_db
        init_db()
        from mongoose.connectors.microsoft_graph import _effective_client_id, _effective_authority
        assert _effective_client_id() == "00000000-1111-2222-3333-444444444444"
        assert "consumers" not in _effective_authority()
    finally:
        os.environ.pop("MS_CLIENT_ID", None)
        import mongoose.settings as S2
        if hasattr(S2, "_settings"):
            S2._settings = None
        if hasattr(S2.get_settings, "cache_clear"):
            S2.get_settings.cache_clear()


def test_background_todo_sync_returns_clean_auth_error_when_not_signed_in():
    _fresh()
    from mongoose.connectors.microsoft_graph import sync_microsoft_todo
    r = sync_microsoft_todo(allow_interactive=False)
    assert isinstance(r, dict)
    assert r["ok"] is False
    assert r.get("error") == "auth"
    assert r.get("needs_signin") is True
    # never a raw stack trace; a human message
    assert isinstance(r.get("detail"), str) and r["detail"]


def test_device_signin_status_idle_by_default():
    _fresh()
    from mongoose.connectors.microsoft_graph import device_signin_status
    assert device_signin_status().get("state") == "idle"


def test_auth_state_shows_needs_signin_without_client_id():
    _fresh()
    from mongoose.setup_wizard import _microsoft_auth_state
    # To Do works via well-known client, so a sign-in is always offered
    assert _microsoft_auth_state({}) == "needs_signin"


# ---- Hardening: throttling, friendly summaries, auto-sync gating ----

class _FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = 0

    def get(self, url, headers=None):
        r = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return r


def test_graph_paged_retries_429_then_succeeds():
    from mongoose.connectors.microsoft_graph import _graph_paged
    client = _FakeClient([
        _FakeResp(429, headers={"Retry-After": "0"}),
        _FakeResp(200, {"value": [{"id": "1"}, {"id": "2"}]}),
    ])
    items, warnings = _graph_paged(client, "http://x/lists", {}, 100, "todo lists")
    assert len(items) == 2
    assert warnings == []          # recovered cleanly, no warning
    assert client.calls >= 2       # it retried


def test_graph_paged_persistent_503_gives_clean_warning_not_hang():
    from mongoose.connectors.microsoft_graph import _graph_paged
    client = _FakeClient([_FakeResp(503)] * 6)
    items, warnings = _graph_paged(client, "http://x/lists", {}, 100, "todo lists")
    assert items == []
    assert any("throttl" in w or "unavailable" in w for w in warnings)


def test_graph_paged_401_raises_auth_error():
    from mongoose.connectors.microsoft_graph import _graph_paged, MicrosoftAuthError
    import pytest
    with pytest.raises(MicrosoftAuthError):
        _graph_paged(_FakeClient([_FakeResp(401)]), "http://x/lists", {}, 100, "todo lists")


def test_todo_summary_is_human_and_plural_aware():
    from mongoose.connectors.microsoft_graph import _todo_summary
    assert _todo_summary({"ok": True, "count": 23, "lists_count": 4}) == "Signed in and pulled 23 tasks from 4 lists."
    assert _todo_summary({"ok": True, "count": 1, "lists_count": 1}) == "Signed in and pulled 1 task from 1 list."
    assert "No open tasks" in _todo_summary({"ok": True, "count": 0})
    assert "sign in again" in _todo_summary({"ok": False, "needs_signin": True}).lower()


def test_scheduler_auto_sync_ready_on_token_not_client_id():
    """The no-registration user signs in via the well-known client and never sets MS_CLIENT_ID, so
    auto-sync must gate on TOKEN presence, not the client id , else their tasks would never refresh."""
    import os
    import tempfile
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    cache = os.path.join(d, "ms.bin")
    os.environ["MS_TOKEN_CACHE"] = cache
    os.environ.pop("MS_CLIENT_ID", None)
    import mongoose.settings as S
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    from mongoose.services.scheduler import _provider_ready
    assert _provider_ready("microsoft") is False     # not signed in yet
    # Realistic-size payload: the plausibility guard rejects tiny placeholder files
    # (a bare {} can never be a real MSAL token cache), so the fixture must look real.
    open(cache, "w").write('{"AccessToken": {"x": "' + 'a' * 200 + '"}}')  # signed in (token cached)
    if hasattr(S, "_settings"):
        S._settings = None
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    assert _provider_ready("microsoft") is True        # now auto-sync runs , even with no client id
    os.environ.pop("MS_TOKEN_CACHE", None)
