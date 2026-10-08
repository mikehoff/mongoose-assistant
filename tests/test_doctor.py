from mongoose.doctor import run_doctor


def test_doctor_returns_non_destructive_report(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "mongoose.sqlite3"))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    out = run_doctor(timeout_seconds=0.01)
    assert "checks" in out
    assert "ollama" in out["checks"]
    assert out["checks"]["ollama"]["ok"] is False
    assert "cloud_escalation" in out["checks"]
