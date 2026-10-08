"""The cockpit ribbon search must resolve the queries a real person actually types.

This is the regression guard for the discoverability audit: before the fix, 28 of 43 realistic
queries returned zero results (the ribbon was shipped but never user-tested). This test extracts the
ribbon index from the served /ui HTML and re-runs the exact AND-substring matcher the JS uses, then
asserts a high pass rate over a battery of natural-language queries.
"""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from mongoose.app import app


def _ribbon_entries(html: str):
    """Parse the {label, kw} pairs out of the buildEntries() index in the served HTML."""
    # entries look like: {label:"Guests & invites", tab:"devices", el:"guestInvite", kw:"guest ..."}
    entries = []
    for m in re.finditer(r'\{label:"([^"]+)",\s*tab:"([^"]+)"(?:,\s*el:"[^"]*")?,\s*kw:"([^"]+)"', html):
        label, tab, kw = m.group(1), m.group(2), m.group(3)
        entries.append({"label": label, "tab": tab, "kw": kw.lower()})
    # plus the per-tab Menu entries built from TABS+LABELS: approximate by adding tab labels
    return entries


def _match(entries, q):
    q = q.strip().lower()
    if not q:
        return []
    terms = q.split()
    return [e for e in entries if all(t in e["kw"] for t in terms)]


REALISTIC_QUERIES = [
    "stop a job", "free memory", "pay", "receipt", "guest", "add guest", "invite",
    "reconcile", "trust", "reserve", "find a file", "search documents", "gmail",
    "calendar", "home assistant", "turn off lights", "update", "upgrade", "benchmark",
    "backup", "voicemail", "pair", "logs", "whatsapp", "music", "doctor", "account",
    "remote access", "away from home", "dyndns", "https", "schedule", "notification",
    "connectors", "deploy", "approvals", "models",
]


def test_ribbon_resolves_realistic_queries():
    c = TestClient(app)
    html = c.get("/ui").text
    entries = _ribbon_entries(html)
    assert len(entries) >= 20, f"ribbon index too small ({len(entries)} entries); did the index break?"
    dead = [q for q in REALISTIC_QUERIES if not _match(entries, q)]
    # Allow at most a couple of misses; the pre-fix state had 28 dead out of a similar set.
    assert len(dead) <= 2, f"{len(dead)} realistic queries return no ribbon match: {dead}"


def test_ribbon_critical_queries_never_dead():
    """The use-cases that came straight from real complaints must always resolve."""
    c = TestClient(app)
    entries = _ribbon_entries(c.get("/ui").text)
    must_resolve = ["stop a job", "free memory", "guest", "pay", "find a file",
                    "remote access", "turn off lights", "backup"]
    for q in must_resolve:
        assert _match(entries, q), f"critical ribbon query is dead: '{q}'"
