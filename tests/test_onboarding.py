"""Tests for the onboarding spine: the single guided first-run journey. The point is that
it's a real STATE MACHINE over live signals (resumable, honest), not a static script, and
that the phone handoff is part of the same flow."""
from fastapi.testclient import TestClient

from mongoose.app import app
from mongoose.db import init_db
import mongoose.onboarding as O

client = TestClient(app)


def test_first_step_is_brain_when_nothing_ready(monkeypatch):
    init_db()
    monkeypatch.setattr(O, "_brain_ready", lambda: (False, "Ollama not reachable"))
    monkeypatch.setattr(O, "_connected_count", lambda: 0)
    monkeypatch.setattr(O, "_paired_count", lambda: 0)
    s = O.onboarding_state()
    assert s["current_step"] == O.STEP_BRAIN
    assert s["complete"] is False
    assert s["progress_pct"] == 0


def test_advances_to_connect_when_brain_ready(monkeypatch):
    init_db()
    monkeypatch.setattr(O, "_brain_ready", lambda: (True, ""))
    monkeypatch.setattr(O, "_connected_count", lambda: 0)
    monkeypatch.setattr(O, "_paired_count", lambda: 0)
    assert O.onboarding_state()["current_step"] == O.STEP_CONNECT


def test_advances_to_phone_when_data_connected(monkeypatch):
    init_db()
    monkeypatch.setattr(O, "_brain_ready", lambda: (True, ""))
    monkeypatch.setattr(O, "_connected_count", lambda: 2)
    monkeypatch.setattr(O, "_paired_count", lambda: 0)
    s = O.onboarding_state()
    assert s["current_step"] == O.STEP_PHONE
    # the phone step is present and not yet done (the handoff is part of THIS flow)
    phone = [st for st in s["steps"] if st["key"] == O.STEP_PHONE][0]
    assert phone["done"] is False


def test_complete_when_phone_paired(monkeypatch):
    init_db()
    monkeypatch.setattr(O, "_brain_ready", lambda: (True, ""))
    monkeypatch.setattr(O, "_connected_count", lambda: 1)
    monkeypatch.setattr(O, "_paired_count", lambda: 1)
    s = O.onboarding_state()
    assert s["current_step"] == O.STEP_DONE
    assert s["complete"] is True
    assert s["progress_pct"] == 100


def test_resumable_phone_only_path(monkeypatch):
    """Brain ready and a phone already paired but no data: still complete (the person
    chose phone-first); the flow doesn't trap them on 'connect'."""
    init_db()
    monkeypatch.setattr(O, "_brain_ready", lambda: (True, ""))
    monkeypatch.setattr(O, "_connected_count", lambda: 0)
    monkeypatch.setattr(O, "_paired_count", lambda: 1)
    assert O.onboarding_state()["current_step"] == O.STEP_DONE


def test_welcome_page_served_and_public():
    r = client.get("/welcome")
    assert r.status_code == 200
    assert "first run" in r.text          # the guided flow, not the config grid
    assert "startPair" in r.text          # the phone handoff is in this flow


def test_onboarding_endpoint():
    r = client.get("/cockpit/onboarding")
    assert r.status_code == 200
    body = r.json()
    assert "current_step" in body and "steps" in body and "next_action" in body
