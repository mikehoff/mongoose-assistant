from pathlib import Path

from mongoose.setup_wizard import merge_new_env_keys


def test_merge_adds_new_keys_preserves_existing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text("OLLAMA_KEEP_ALIVE=10m\nHA_TOKEN=secret\n")
    Path(".env.example").write_text(
        "OLLAMA_KEEP_ALIVE=10m\n# new feature\nMEMORY_AUTO_EXTRACT=true\nNEW_KEY=\n"
    )
    added = merge_new_env_keys(Path(".env"), Path(".env.example"))
    assert set(added) == {"MEMORY_AUTO_EXTRACT", "NEW_KEY"}
    text = Path(".env").read_text()
    assert text.count("OLLAMA_KEEP_ALIVE") == 1   # not duplicated
    assert "OLLAMA_KEEP_ALIVE=10m" in text        # existing value preserved
    assert "HA_TOKEN=secret" in text              # untouched
    assert "MEMORY_AUTO_EXTRACT=true" in text     # new key added with default
    assert "# new feature" in text                # comment carried over


def test_merge_noop_when_nothing_new(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text("A=1\nB=2\n")
    Path(".env.example").write_text("A=9\nB=9\n")
    added = merge_new_env_keys(Path(".env"), Path(".env.example"))
    assert added == []
    # existing values must NOT be overwritten by the example's
    assert "A=1" in Path(".env").read_text()


def test_merge_handles_missing_example(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text("A=1\n")
    assert merge_new_env_keys(Path(".env"), Path("nope.example")) == []


def test_new_defaults_are_useful():
    # The shipped defaults should match what we actually found useful.
    import os
    from mongoose.settings import get_settings
    for k in ("OLLAMA_KEEP_ALIVE", "MEMORY_AUTO_EXTRACT", "OLLAMA_THINK"):
        os.environ.pop(k, None)
    s = get_settings()
    assert s.ollama_keep_alive == "-1"
    assert s.keep_alive_value == -1  # sent to Ollama as an int, not a string
    assert s.ollama_think is False
    # Model-assisted auto-extract now defaults OFF: on a base M4 (one model at a time) it caused a
    # SECOND model inference per chat turn, doubling latency and contributing to freezes. Deterministic
    # preference capture and forgetting still work without it; users can opt in with MEMORY_AUTO_EXTRACT.
    assert s.memory_auto_extract is False


def test_dedupe_env_collapses_duplicates(tmp_path, monkeypatch):
    from mongoose.setup_wizard import dedupe_env
    env = tmp_path / ".env"
    env.write_text(
        "A=1\n# c\nKEEP=10m\nB=2\nKEEP=-1\nDUP=x\nDUP=y\n"
    )
    removed = dedupe_env(env)
    text = env.read_text()
    # each key once, last value kept, comment preserved
    assert text.count("KEEP=") == 1 and "KEEP=-1" in text
    assert text.count("DUP=") == 1 and "DUP=y" in text
    assert "# c" in text
    assert set(removed) == {"KEEP", "DUP"}
    # idempotent
    assert dedupe_env(env) == []


def test_dedupe_env_keeps_first_position(tmp_path):
    from mongoose.setup_wizard import dedupe_env
    env = tmp_path / ".env"
    env.write_text("KEEP=1\nOTHER=z\nKEEP=2\n")
    dedupe_env(env)
    lines = [l for l in env.read_text().splitlines() if "=" in l]
    assert lines.index("KEEP=2") < lines.index("OTHER=z")


def test_write_env_self_heals_duplicates(tmp_path):
    from mongoose.setup_wizard import write_env
    env = tmp_path / ".env"
    env.write_text("DUP=1\nDUP=2\nKEEP=x\n")
    write_env({"KEEP": "y"}, env)
    text = env.read_text()
    assert text.count("DUP=") == 1 and "DUP=2" in text
    assert "KEEP=y" in text
