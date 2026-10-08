

def test_stable_env_migrates_from_previous_sibling_install(tmp_path, monkeypatch):
    """A reinstall from a new extracted folder must not orphan the old install-local .env."""
    old_install = tmp_path / "mongoose-1.1.18"
    new_install = tmp_path / "mongoose-1.1.19"
    stable_dir = tmp_path / "stable-config"
    old_install.mkdir()
    new_install.mkdir()
    (old_install / ".env").write_text(
        "IMAP_HOST=imap.example.net\n"
        "IMAP_USER=alex@example.net\n"
        "DUCKDNS_DOMAIN=mongoose-test\n",
        encoding="utf-8",
    )
    (new_install / ".env").write_text("UNRELATED_APP=not-mongoose\n", encoding="utf-8")

    monkeypatch.setenv("MONGOOSE_CONFIG_DIR", str(stable_dir))
    monkeypatch.delenv("MONGOOSE_ENV_FILE", raising=False)
    monkeypatch.chdir(new_install)

    import mongoose.setup_wizard as SW

    env_path = SW._resolve_env_path()
    assert env_path == stable_dir / ".env"
    text = env_path.read_text(encoding="utf-8")
    assert "IMAP_HOST=imap.example.net" in text
    assert "DUCKDNS_DOMAIN=mongoose-test" in text
    assert "UNRELATED_APP" not in text


def test_stable_env_migration_does_not_overwrite_existing_stable_file(tmp_path, monkeypatch):
    old_install = tmp_path / "mongoose-old"
    new_install = tmp_path / "mongoose-new"
    stable_dir = tmp_path / "stable-config"
    old_install.mkdir()
    new_install.mkdir()
    stable_dir.mkdir()
    (old_install / ".env").write_text("IMAP_HOST=old.example.net\n", encoding="utf-8")
    (stable_dir / ".env").write_text("IMAP_HOST=stable.example.net\n", encoding="utf-8")

    monkeypatch.setenv("MONGOOSE_CONFIG_DIR", str(stable_dir))
    monkeypatch.delenv("MONGOOSE_ENV_FILE", raising=False)
    monkeypatch.chdir(new_install)

    import mongoose.setup_wizard as SW

    env_path = SW._resolve_env_path()
    assert env_path == stable_dir / ".env"
    assert env_path.read_text(encoding="utf-8") == "IMAP_HOST=stable.example.net\n"
