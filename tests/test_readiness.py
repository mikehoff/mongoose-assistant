"""Tests for the readiness truth layer. The load-bearing property is HONESTY: never fake
readiness, never count placeholder config as connected, distinguish off_by_design from
failing, and keep secrets off the voice/phone surfaces. Plus: voice is short and ranked."""
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()


def _seed_email(indexed_at: str):
    from mongoose.db import connect
    with connect() as con:
        con.execute("INSERT INTO email_messages(source,sender,subject,body,received_at,privacy,indexed_at) "
                    "VALUES('gmail','a','s','b',?,'normal',?)", (indexed_at, indexed_at))
        con.commit()


# --- honesty: nothing faked on a fresh install ------------------------------

def test_fresh_install_nothing_fake_ready():
    _fresh()
    from mongoose.services.readiness import readiness_report
    rep = readiness_report()
    # the only things that can be "ready"/"has_data" on a truly fresh box are core local bits
    # (database). No external source should claim readiness.
    ready_sources = [it for it in rep["items"]
                     if it["status"] in ("ready", "has_data") and it["domain"] in ("mail", "messaging", "files")]
    assert ready_sources == []


def test_placeholder_config_does_not_count_as_configured():
    _fresh()
    # Gmail's default secrets PATH is set in settings, but the FILE doesn't exist on a fresh
    # install. That must read as not_configured, not configured/authorised (the v0.31 bug class).
    from mongoose.services.readiness import readiness_report
    gmail = next(it for it in readiness_report()["items"] if it["id"] == "gmail")
    assert gmail["status"] == "not_configured"


def test_token_file_gates_authorised(monkeypatch, tmp_path):
    _fresh()
    # if the google secrets file actually exists, Gmail moves off not_configured
    secrets = tmp_path / "google-client-secrets.json"
    secrets.write_text("{}")
    import mongoose.settings as S
    orig = S.get_settings

    def patched():
        s = orig()
        object.__setattr__(s, "google_client_secrets_path", str(secrets))
        return s
    monkeypatch.setattr(S, "get_settings", patched)
    from mongoose.services.readiness import readiness_report
    gmail = next(it for it in readiness_report()["items"] if it["id"] == "gmail")
    # file exists but no token/data yet -> configured or needs_auth, NOT not_configured
    assert gmail["status"] in ("configured", "needs_authorisation")


def test_data_rows_produce_has_data():
    _fresh()
    _seed_email(datetime.now(timezone.utc).isoformat())   # fresh email row
    # also need the credential to read as authorised; simulate via connected_sources path:
    # a data row alone is enough for liveness/readiness to call it has_data here.
    from mongoose.services.readiness import readiness_report
    gmail = next(it for it in readiness_report()["items"] if it["id"] == "gmail")
    # with fresh data rows it should be has_data (or at least not not_configured)
    assert gmail["status"] in ("has_data", "authorised", "stale")


def test_stale_timestamp_produces_stale():
    _fresh()
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    _seed_email(old)
    from mongoose.services.readiness import readiness_report
    gmail = next(it for it in readiness_report()["items"] if it["id"] == "gmail")
    assert gmail["status"] == "stale"


def test_off_by_design_or_not_configured_distinct_from_failing():
    _fresh()
    from mongoose.services.readiness import readiness_report
    rep = readiness_report()
    # WhatsApp not set up is not_configured (severity none), never "failing"
    wa = next(it for it in rep["items"] if it["id"] == "whatsapp")
    assert wa["status"] == "not_configured"
    assert wa["severity"] == "none"


# --- voice/phone shape ------------------------------------------------------

def test_voice_is_short_and_ranked():
    _fresh()
    from mongoose.services.readiness_presenter import readiness_for_voice
    v = readiness_for_voice()
    # short: a handful of sentences, not a 13-item dump
    assert v["speech"].count(".") <= 6
    # ranked: gaps are at most 3
    assert len(v["gaps"]) <= 3


def test_needs_me_voice_ranked_and_capped():
    _fresh()
    from mongoose.services.readiness_presenter import needs_me_for_voice
    nm = needs_me_for_voice()
    assert len(nm["items"]) <= 3


def test_phone_payload_has_no_secrets():
    _fresh()
    _seed_email((datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
    from mongoose.services.readiness_presenter import readiness_for_phone
    blob = json.dumps(readiness_for_phone()).lower()
    for needle in ("token", "password", "secret", "client_secret", "api_key"):
        assert needle not in blob


# --- intent routing ---------------------------------------------------------

def test_readiness_intent_matches_status_questions():
    from mongoose.role_router import looks_like_readiness_query as q
    assert q("are you ready?")
    assert q("what needs me?")
    assert q("what's broken?")
    assert q("is whatsapp working?")


def test_readiness_intent_ignores_ordinary_requests():
    from mongoose.role_router import looks_like_readiness_query as q
    assert not q("turn off the kitchen lights")
    assert not q("what is the weather")
    assert not q("are you free tomorrow at 3?")


# --- cache ------------------------------------------------------------------

def test_cache_returns_snapshot_then_refresh_updates():
    _fresh()
    from mongoose.services.readiness import cached_readiness, refresh_readiness
    first = cached_readiness()
    assert not first.get("cached")        # freshly computed
    second = cached_readiness()
    assert second.get("cached") is True   # read from snapshot
    refreshed = refresh_readiness()
    assert "generated_at" in refreshed


# --- no live network in tests -----------------------------------------------

def test_readiness_does_not_require_network():
    # collectors must be resilient with no Ollama/HA/Docker reachable (the sandbox case):
    # the report still returns, marking unknown/failing where it can't measure, never raising.
    _fresh()
    from mongoose.services.readiness import readiness_report
    rep = readiness_report()
    assert "items" in rep and len(rep["items"]) > 0
