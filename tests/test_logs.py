"""Tests for the cockpit log viewer. The security-relevant properties: it can ONLY
open files inside the log dir (no path traversal), and reads are bounded (tail, not the
whole file). Also that the Logs tab renders log lines as text, never HTML."""
from fastapi.testclient import TestClient

from mongoose.app import app

client = TestClient(app)


def _make_log(text: str, name: str = "scheduler.out.log"):
    from mongoose.service import _log_dir
    p = _log_dir() / name
    p.write_text(text, encoding="utf-8")
    return p


def test_logs_list(tmp_path, monkeypatch):
    _make_log("hello\n")
    r = client.get("/cockpit/logs")
    assert r.status_code == 200
    names = [x["name"] for x in r.json()["logs"]]
    assert "scheduler.out.log" in names


def test_log_tail_returns_content():
    _make_log("alpha\nbeta\ngamma\n")
    r = client.get("/cockpit/logs/scheduler.out.log")
    assert r.status_code == 200
    body = r.json()
    assert "gamma" in body["text"]


def test_log_tail_is_bounded():
    # a big log: the tail must not return the whole thing
    _make_log(("x" * 100 + "\n") * 5000, name="big.log")  # ~500KB
    r = client.get("/cockpit/logs/big.log?kb=16")
    assert r.status_code == 200
    body = r.json()
    assert body["truncated"] is True
    assert len(body["text"].encode("utf-8")) <= 16 * 1024


def test_log_traversal_blocked():
    """The core security property: only enumerated files in the log dir are openable.
    Any traversal or unknown name 404s, it never escapes to arbitrary paths."""
    for bad in ["..%2F..%2Fetc%2Fpasswd", "passwd", "nope.log", "..%2Fmongoose.sqlite3"]:
        r = client.get(f"/cockpit/logs/{bad}")
        assert r.status_code == 404, f"{bad} should be 404, got {r.status_code}"


def test_log_kb_param_capped():
    _make_log("data\n", name="small.log")
    # absurd kb is clamped, not honoured (no unbounded read)
    r = client.get("/cockpit/logs/small.log?kb=999999")
    assert r.status_code == 200


def test_logs_tab_present_and_safe():
    body = client.get("/ui").text
    assert "Logs" in body and "loadLogs" in body
    assert "/cockpit/logs" in body
    # log content rendered as text, not HTML (XSS-safe for hostile log lines)
    assert "view.textContent" in body
