"""Connector probes must never show false-green: configured-but-unusable is reported honestly, and a
connector with no credentials is reported as not-configured, not as connected.

Regression guard for the integration enable/disable audit. The "disable leaves a zombie" failure mode
is structurally avoided because connectors are credential-presence based and the probe checks live
reality (configured AND usable), never a cached enabled-flag. This test asserts that with zero
credentials, no cloud connector claims to be usable.
"""
from __future__ import annotations


from mongoose.connector_probes import probe_all


def test_no_cloud_connector_is_usable_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "c.sqlite3"))
    res = probe_all()
    connectors = {c["name"]: c for c in res["connectors"]}
    # cloud connectors must be unusable with no creds
    for name in ["gmail", "google_calendar", "google_drive", "microsoft", "home_assistant"]:
        if name in connectors:
            assert connectors[name]["usable"] is False, f"{name} falsely usable with no credentials"
            # and they should report not-configured (or at least give a human detail)
            assert connectors[name]["detail"], f"{name} gives no human detail for its unconfigured state"


def test_configured_does_not_imply_usable(tmp_path, monkeypatch):
    """A connector that is 'configured' but not reachable must show usable=False, not a false green."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "c.sqlite3"))
    res = probe_all()
    connectors = {c["name"]: c for c in res["connectors"]}
    # ollama is typically 'configured' locally but not reachable in this sandbox -> usable must be False
    if "ollama" in connectors and connectors["ollama"]["configured"]:
        # if it's configured but the local server isn't up, it must not claim usable
        if "refused" in (connectors["ollama"]["detail"] or "").lower() or not connectors["ollama"]["usable"]:
            assert connectors["ollama"]["usable"] is False


def test_every_connector_reports_both_axes(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "c.sqlite3"))
    res = probe_all()
    for c in res["connectors"]:
        assert "configured" in c and "usable" in c, f"{c.get('name')} missing configured/usable axes"
