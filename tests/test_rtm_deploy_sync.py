"""RTM deploy-state sync record , the shared spine that keeps the RTM screen, the deploy, the chat plan
and the live example-court.org site from drifting apart.

These tests pin the behaviour that makes the sync strip trustworthy: the verdict is derived from the
recorded facts (so it can never lie), each field has exactly one writer (so the record can never
contradict itself), and the webhook actually records the live site's reported version (so the loop
closes and the strip can go green).

DB isolation is automatic via conftest.
"""
from __future__ import annotations


def test_verdict_unknown_when_nothing_deployed():
    from mongoose.services import rtm_deploy_state as D
    s = D.sync_status()
    assert s["level"] == "unknown"
    assert s["deployed_version"] is None


def test_verdict_amber_when_plan_approved_not_deployed():
    from mongoose.services import rtm_deploy_state as D
    D.record_approved_plan(plan_id="p1", name="Leaseholder upload", step_count=4)
    s = D.sync_status()
    assert s["level"] == "amber"
    assert "approved" in s["headline"].lower()


def test_verdict_amber_while_awaiting_live_confirmation():
    from mongoose.services import rtm_deploy_state as D
    D.record_deployed(version="2.4.0", plan_id="p1")
    s = D.sync_status()
    assert s["level"] == "amber"
    assert "2.4.0" in s["headline"]


def test_verdict_green_when_live_confirms_same_version():
    from mongoose.services import rtm_deploy_state as D
    D.record_deployed(version="2.4.0", plan_id="p1")
    D.record_live_confirmation(version="2.4.0")
    s = D.sync_status()
    assert s["level"] == "green"
    assert s["deployed_version"] == "2.4.0"
    assert s["live_version"] == "2.4.0"


def test_verdict_red_on_version_drift():
    from mongoose.services import rtm_deploy_state as D
    D.record_deployed(version="2.4.0")
    D.record_live_confirmation(version="2.4.0")  # green
    D.record_deployed(version="2.5.0")           # new deploy, live still on 2.4.0
    s = D.sync_status()
    assert s["level"] == "red"
    assert "out of sync" in s["headline"].lower()


def test_deployed_is_only_writer_of_deployed_version():
    """The verdict must reflect what was actually deployed, never an approved-but-undeployed version."""
    from mongoose.services import rtm_deploy_state as D
    D.record_approved_plan(plan_id="p9", name="Big feature")
    s = D.sync_status()
    # approving a plan must NOT set deployed_version
    assert s["deployed_version"] is None
    assert s["level"] == "amber"  # approved, not deployed


def test_webhook_deployment_confirmed_records_live_version():
    """The inbound webhook closes the loop: a deployment_confirmed event from the live site records its
    running version into the shared record so the strip can confirm sync."""
    from mongoose.services.webhook import ingest_webhook_event
    from mongoose.services import rtm_deploy_state as D
    ingest_webhook_event("deployment_confirmed", "example-court.org", {"version": "3.1.0"})
    assert D.sync_status()["live_version"] == "3.1.0"


def test_webhook_accepts_alternate_version_keys():
    from mongoose.services.webhook import ingest_webhook_event
    from mongoose.services import rtm_deploy_state as D
    ingest_webhook_event("plugin_updated", "example-court.org", {"pc_rtm_version": "3.2.0"})
    assert D.sync_status()["live_version"] == "3.2.0"


def test_pending_plan_surfaced_even_when_deployed():
    """An approved plan newer than what is deployed is surfaced so the owner sees there is work to ship."""
    from mongoose.services import rtm_deploy_state as D
    D.record_deployed(version="1.0.0", plan_id="old")
    D.record_live_confirmation(version="1.0.0")
    D.record_approved_plan(plan_id="new", name="Next feature")
    s = D.sync_status()
    assert s["pending_plan"] is not None
    assert s["pending_plan"]["name"] == "Next feature"
