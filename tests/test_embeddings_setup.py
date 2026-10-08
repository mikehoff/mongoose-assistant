"""The setup-embeddings command downloads and installs the sentence-transformers model.
This tests the CLI registration, the download path, and verification."""
from __future__ import annotations

from pathlib import Path
from unittest import mock


def test_setup_embeddings_command_in_cli():
    """The command is registered and shows up in the help."""
    from mongoose.cli import main
    import sys
    import io
    old_argv = sys.argv
    old_stdout = sys.stdout
    try:
        sys.argv = ["mongoose", "-h"]
        sys.stdout = io.StringIO()
        try:
            main()
        except SystemExit:
            pass
        output = sys.stdout.getvalue()
        assert "setup-embeddings" in output, output
    finally:
        sys.argv = old_argv
        sys.stdout = old_stdout


def test_embeddings_missing_status_hints_setup_command():
    """When embeddings are missing, the availability check hints at 'mongoose setup-embeddings'."""
    from mongoose.services.embeddings import available
    with mock.patch("mongoose.services.embeddings.model_dir") as md:
        fake_dir = Path("/fake/nonexistent/dir/that/will/never/exist")
        md.return_value = fake_dir
        status = available()
        assert not status["ok"]
        assert "setup-embeddings" in status["hint"]


def test_embeddings_imports_work_when_installed():
    """If the model files exist, the imports work (or gracefully fail with None)."""
    try:
        from mongoose.services.embeddings import available, embed
        status = available()
        # Either ok or it says what's missing
        assert "ok" in status and isinstance(status["ok"], bool)
        # embed should return None gracefully if model isn't there
        result = embed(["test"])
        assert result is None or (isinstance(result, list) and all(isinstance(r, list) for r in result))
    except Exception as e:
        # Optional dependency, so import failure is ok
        assert "onnxruntime" in str(e) or "tokenizers" in str(e) or "numpy" in str(e)


def test_capability_search_falls_back_to_keywords_without_embeddings():
    """The capability_search tool works even if embeddings model isn't installed."""
    from mongoose.services.capability_index import search
    res = search("sync mail")
    assert res
    assert all("signal" in r for r in res)
    # Should say "keywords" if no model
    if res:
        assert res[0]["signal"] in ("keywords", "hybrid")


def test_ensure_model_skips_existing_files_and_never_touches_network(tmp_path, monkeypatch):
    """Idempotence: files present means zero downloads, zero network. This is what makes it
    safe to call from every startup and every update."""
    import mongoose.services.embeddings as E
    monkeypatch.setattr(E, "model_dir", lambda: tmp_path)
    (tmp_path / "model.onnx").write_bytes(b"x")
    (tmp_path / "tokenizer.json").write_bytes(b"x")
    import urllib.request as ur
    def boom(*a, **k):
        raise AssertionError("network touched despite files present")
    monkeypatch.setattr(ur, "urlretrieve", boom)
    res = E.ensure_model(quiet=True)
    assert res["ok"] and not res["downloaded"] and set(res["skipped"]) == {"model.onnx", "tokenizer.json"}


def test_ensure_model_respects_opt_out(tmp_path, monkeypatch):
    import mongoose.services.embeddings as E
    monkeypatch.setattr(E, "model_dir", lambda: tmp_path)
    monkeypatch.setenv("MONGOOSE_AUTO_EMBEDDINGS", "0")
    res = E.ensure_model(quiet=True)
    assert not res["ok"] and "disabled" in res["error"]


def test_update_script_fetches_the_model():
    """The model is part of the normal update, not a separate action: update.sh must call
    ensure_model after pip install, non-fatally."""
    from pathlib import Path
    sh = Path("scripts/update.sh").read_text()
    assert "ensure_model" in sh
    i = sh.find("pip install -q -e .")
    assert 0 < i < sh.find("ensure_model"), "model fetch must come after the pip install"


def test_startup_fetches_model_in_background_thread():
    """Boot never waits on a 500MB download: the lifespan starts a daemon thread when files
    are missing. Pin the wiring textually (executing lifespan needs a running loop)."""
    from pathlib import Path
    src = Path("src/mongoose/app.py").read_text()
    i = src.find("async def lifespan")
    seg = src[i:i + 4000]
    assert "ensure_model" in seg or "_emb_ensure" in seg
    assert "daemon=True" in seg
