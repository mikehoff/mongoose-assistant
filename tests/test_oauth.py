from mongoose.oauth import authorize_google, authorize_microsoft, oauth_status


def test_google_without_secrets_fails_gracefully(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_SECRETS_PATH", str(tmp_path / "nope.json"))
    ok, msg = authorize_google()
    assert ok is False
    assert "not found" in msg.lower()


def test_microsoft_without_client_id_fails_gracefully(monkeypatch):
    monkeypatch.delenv("MS_CLIENT_ID", raising=False)
    # To Do now works WITHOUT a registered client (well-known public client fallback), so authorize no
    # longer demands MS_CLIENT_ID. Without network it returns a clean failure, but never an
    # "ms_client_id not set" refusal , the whole point of the no-registration path.
    ok, msg = authorize_microsoft(todo_only=True)
    assert ok is False  # no real sign-in happens in tests (no network / no user)
    assert "ms_client_id" not in msg.lower()
    assert "register an app" not in msg.lower()


def test_oauth_status_reports_token_presence(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "g.json"))
    monkeypatch.setenv("MS_TOKEN_CACHE", str(tmp_path / "m.bin"))
    st = oauth_status()
    assert st == {"google": False, "microsoft": False}
    (tmp_path / "g.json").write_text('{"token": "ya29.' + 'a' * 200 + '"}')
    assert oauth_status()["google"] is True
