"""Tests for the system map (transparency view): the honest 'what IS Mongoose' answer.

The point is that it tells the TRUTH from real state: real data counts, the real storage
location, and , most importantly , an honest egress map that says nothing leaves by
default but flags every real external flow. The privacy claim must be verifiable, not
asserted, so these tests guard against both crying wolf and hiding real egress."""
from fastapi.testclient import TestClient

from mongoose.app import app
from mongoose.db import connect, init_db
import mongoose.transparency as T

client = TestClient(app)


def test_clean_install_nothing_leaves_passively(monkeypatch):
    """A fresh install must honestly report that nothing about the user's content leaves
    the machine ON ITS OWN. Web search ships on (local instance) but only sends data when
    the user actually searches, so passive egress is still nothing."""
    monkeypatch.delenv("MONGOOSE_SEARXNG_URL", raising=False)
    monkeypatch.delenv("CLOUD_ESCALATION_ENABLED", raising=False)
    import mongoose.settings as st
    st._SETTINGS = None
    init_db()
    m = T.system_map()
    assert m["egress"]["anything_leaves"] is False           # nothing leaves passively
    assert "on its own" in m["egress"]["summary"].lower() or \
           "nothing about your content leaves" in m["egress"]["summary"].lower()
    # the model flow is present and local
    model = [f for f in m["egress"]["flows"] if "model" in f["name"].lower()][0]
    assert model["leaves_machine"] is False


def test_default_web_search_is_disclosed_honestly(monkeypatch):
    """Web search ships on by default (local SearXNG). Even though the INSTANCE is local,
    the map must disclose that searching sends terms to the internet, not hide it."""
    monkeypatch.delenv("MONGOOSE_SEARXNG_URL", raising=False)  # use the shipped default
    import mongoose.settings as st
    st._SETTINGS = None
    init_db()
    m = T.system_map()
    search = [f for f in m["egress"]["flows"] if "search" in f["name"].lower()]
    assert search, "web search must appear in the egress map by default"
    assert search[0]["leaves_machine"] is True                 # searching reaches the internet
    assert m["egress"]["anything_leaves_on_action"] is True
    # but it's clearly framed as on-action + via the local instance
    assert "internet" in search[0]["note"].lower() and "this machine" in search[0]["note"].lower()


def test_external_searxng_discloses_third_party(monkeypatch):
    """An EXTERNAL SearXNG must additionally warn that the third-party server sees terms."""
    monkeypatch.setenv("MONGOOSE_SEARXNG_URL", "https://searx.example.org")
    import mongoose.settings as st
    st._SETTINGS = None
    init_db()
    m = T.system_map()
    search = [f for f in m["egress"]["flows"] if "search" in f["name"].lower()][0]
    assert search["leaves_machine"] is True
    assert "external" in search["note"].lower()


def test_cloud_escalation_flagged_when_on(monkeypatch):
    """If cloud escalation is enabled, the map must NOT hide it , the model flow leaves."""
    init_db()
    monkeypatch.setenv("CLOUD_ESCALATION_ENABLED", "true")
    import mongoose.settings as st
    st._SETTINGS = None
    m = T.system_map()
    model = [f for f in m["egress"]["flows"] if "model" in f["name"].lower()][0]
    assert model["leaves_machine"] is True
    assert m["egress"]["anything_leaves"] is True




def test_data_counts_are_real():
    init_db()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with connect() as con:
        for _ in range(3):
            con.execute("INSERT INTO email_messages(source,sender,subject,body,received_at,privacy,indexed_at) "
                        "VALUES('t','a@x.com','s','b',?,'normal',?)", (now, now))
        con.commit()
    m = T.system_map()
    email = [c for c in m["data_categories"] if c["category"] == "Email"][0]
    assert email["count"] == 3 and email["present"] is True


def test_storage_shows_real_path():
    init_db()
    m = T.system_map()
    assert m["storage"]["location"]            # the real db path
    assert "machine" in m["storage"]["where"]


def test_surfaces_include_mac_as_brain():
    init_db()
    m = T.system_map()
    mac = [s for s in m["surfaces"] if s["role"] == "brain"][0]
    assert "this Mac" in mac["name"].lower() or "mac" in mac["name"].lower()


def test_paired_device_appears_as_surface():
    init_db()
    import mongoose.devices as D
    D.claim_pairing_code(D.create_pairing_code("primary_phone")["code"], "Test Phone", "android")
    m = T.system_map()
    assert any(s["name"] == "Test Phone" for s in m["surfaces"])


def test_system_map_endpoint_and_tab():
    r = client.get("/cockpit/system-map")
    assert r.status_code == 200
    body = client.get("/ui").text
    assert "system:loadSystem" in body
    assert "What leaves this machine" in body
