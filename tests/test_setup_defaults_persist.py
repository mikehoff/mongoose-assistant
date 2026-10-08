def test_saved_push_defaults_count_as_configured(tmp_path, monkeypatch):
    import mongoose.setup_wizard as SW
    from mongoose.wizard import save_integration

    envf = tmp_path / ".env"
    old = SW.ENV_PATH
    SW.ENV_PATH = envf
    try:
        save_integration("push", {"NTFY_URL": "http://127.0.0.1:8090", "NTFY_TOPIC": "mongoose-private-topic"})
        flat = {i["slug"]: i for i in SW.integration_status()}
        assert flat["push"]["configured"] is True
    finally:
        SW.ENV_PATH = old


def test_saved_scheduler_defaults_count_as_configured(tmp_path, monkeypatch):
    import mongoose.setup_wizard as SW
    from mongoose.wizard import save_integration

    envf = tmp_path / ".env"
    old = SW.ENV_PATH
    SW.ENV_PATH = envf
    try:
        save_integration("scheduler", {"MONGOOSE_BRIEF_TIME": "07:30", "MONGOOSE_EMAIL_SYNC_MINUTES": "30"})
        flat = {i["slug"]: i for i in SW.integration_status()}
        assert flat["scheduler"]["configured"] is True
    finally:
        SW.ENV_PATH = old
