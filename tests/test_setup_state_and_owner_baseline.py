"""Two reviewers converged on the same architectural gap: HTTPS, phone pairing, WhatsApp,
brain-location and embeddings were each computed independently by doctor.py, cockpit's TLS
panel, release_check.py, and Overview, which is exactly how a fix landing in one place never
reached its siblings all night. setup_state.py is the single canonical layer every one of
those now reads from.

Also pins owner_defaults.py's actual scope: an API token, a private ntfy topic, and LAN
reachability (MONGOOSE_HOST=0.0.0.0, MONGOOSE_ALLOW_REMOTE=true) all prepared by default,
because a home assistant the owner's own phone can't reach on the owner's own home network
isn't private, it's broken; the phone is part of the local environment. An earlier version of
this reasoning treated binding to 0.0.0.0 as nearly equivalent to public internet exposure,
which doesn't hold up: 0.0.0.0 means listening on the LAN-facing interface as well as
loopback, and does not cross the router's NAT boundary on its own. That requires a separate,
additional condition (an actual port-forward or UPnP mapping) that this module never creates
and never requests. The residual risk of defaulting LAN-wide is "another device on the same
trusted home network can reach an already-token-protected service", which is the risk profile
this product's stated purpose requires accepting, not a reason to gate the common case behind
a click every real owner will make anyway. Public internet ingress (DDNS, a router
port-forward/UPnP mapping, a publicly-trusted certificate) stays a genuinely different,
separate, deliberate action; this module never touches any of it."""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def _fresh(monkeypatch, tmp_path, **extra):
    env = {"MONGOOSE_DB_PATH": str(tmp_path / "s.sqlite3"), "MONGOOSE_DATA_DIR": str(tmp_path / "d")}
    env.update(extra)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()


def test_setup_state_covers_every_surface(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.setup_state import setup_state
    s = setup_state()["surfaces"]
    for name in ("brain_location", "https", "phone_home", "remote_away", "guest_invites",
                "notifications", "embeddings", "whatsapp"):
        assert name in s, name
        assert set(s[name].keys()) >= {"ok", "status", "detail", "severity", "next_action",
                                       "next_action_url", "facts"}


def test_doctor_and_setup_state_can_never_disagree_about_https(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.doctor import run_doctor
    from mongoose.setup_state import setup_state
    d_ok = run_doctor()["checks"]["https"]["ok"]
    s_ok = setup_state(only=["https"])["surfaces"]["https"]["ok"]
    assert d_ok == s_ok
    from mongoose.self_signed_tls import generate_self_signed
    generate_self_signed()
    d_ok2 = run_doctor()["checks"]["https"]["ok"]
    s_ok2 = setup_state(only=["https"])["surfaces"]["https"]["ok"]
    assert d_ok2 == s_ok2 is True


def test_push_to_phone_and_notifications_are_now_the_same_fact_not_coincidentally_similar(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.doctor import run_doctor
    checks = run_doctor()["checks"]
    assert checks["push_to_phone"]["ok"] == checks["notifications"]["ok"]
    assert checks["push_to_phone"]["detail"] == checks["notifications"]["detail"]


def test_no_circular_import_between_doctor_and_setup_state(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.doctor import run_doctor
    from mongoose.setup_state import setup_state
    run_doctor()
    setup_state()
    run_doctor()


def test_notifications_are_chain_aware_not_circular(monkeypatch, tmp_path):
    """If the phone itself isn't reachable, THAT is the blocker, not '0 subscriptions'
    while the launchpad that would grant permission can't even open."""
    _fresh(monkeypatch, tmp_path)
    from mongoose.setup_state import setup_state
    n = setup_state(only=["notifications"])["surfaces"]["notifications"]
    assert n["status"] == "blocked_by_phone_home"


def test_embeddings_never_shows_the_broken_placeholder_via_setup_state(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from mongoose.setup_state import setup_state
    e = setup_state(only=["embeddings"])["surfaces"]["embeddings"]
    assert "?" not in e["detail"]


def test_no_terminal_instruction_anywhere_in_the_setup_facing_source():
    import pathlib
    banned = ["make_cert.sh", "mongoose whatsapp setup", "mongoose tls obtain",
              "serve --tls) once a cert exists"]
    for fname in ("src/mongoose/doctor.py", "src/mongoose/setup_state.py",
                 "src/mongoose/release_check.py", "src/mongoose/acme_tls.py",
                 "src/mongoose/services/whatsapp_journey.py", "src/mongoose/services/whatsapp.py"):
        text = pathlib.Path(fname).read_text()
        for phrase in banned:
            assert phrase not in text, f"{phrase!r} still in {fname}"


def test_owner_baseline_defaults_lan_reachability_and_token_and_topic(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / "owner.env")
    from mongoose.owner_defaults import ensure_owner_baseline
    r = ensure_owner_baseline(source="test")
    assert r["ok"]
    assert r["updates"].get("MONGOOSE_HOST") == "0.0.0.0"
    assert r["updates"].get("MONGOOSE_ALLOW_REMOTE") == "true"
    assert "MONGOOSE_API_TOKEN" in r["updates"]
    assert "NTFY_TOPIC" in r["updates"] and r["updates"]["NTFY_TOPIC"] != "mongoose-private-topic"
    r2 = ensure_owner_baseline(source="test")
    assert r2["updates"] == {}, "must be idempotent"


def test_owner_baseline_off_opts_out_of_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MONGOOSE_OWNER_BASELINE_OFF", "1")
    import mongoose.setup_wizard as sw
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / "owner.env")
    from mongoose.owner_defaults import ensure_owner_baseline
    r = ensure_owner_baseline(source="test")
    assert r["ok"] and r["updates"] == {} and r.get("skipped")


def test_owner_baseline_never_touches_public_ingress():
    """LAN reachability is the default; public/WAN ingress (DDNS, port-forward, a
    publicly-trusted cert) stays a separate, deliberate action this module never takes.
    Checks for actual imports/calls that would perform those actions, not bare mentions of
    the words, since the function's own docstring legitimately explains this boundary using
    those exact terms."""
    import inspect
    from mongoose.owner_defaults import ensure_owner_baseline
    src = inspect.getsource(ensure_owner_baseline)
    forbidden_imports = ("from .ddns import", "from .acme_tls import", "from .setup_ddns import",
                         "import upnp", "subprocess")
    for forbidden in forbidden_imports:
        assert forbidden not in src, f"{forbidden!r} would perform a public-ingress action"


def test_agent_output_hygiene_drops_narration_keeps_evidence():
    from mongoose.services.agent_output_hygiene import sanitize_agent_output, summarize_stream_json
    raw = "Let me check that.\ntests/test_foo.py PASSED\nHmm, not sure.\nTraceback (most recent call last):\n"
    out = sanitize_agent_output(raw)
    assert "Let me check" not in out and "Hmm, not sure" not in out
    assert "PASSED" in out and "Traceback" in out
    import json
    events = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "maybe I should look at this"},
            {"type": "tool_use", "name": "str_replace", "input": {"path": "app.py"}}]}}),
        json.dumps({"type": "result", "result": "done", "is_error": False}),
    ])
    out2 = summarize_stream_json(events)
    assert "maybe I should" not in out2 and "tool: str_replace app.py" in out2


def test_agent_output_hygiene_never_crashes_on_malformed_input():
    from mongoose.services.agent_output_hygiene import sanitize_agent_output, summarize_stream_json
    assert sanitize_agent_output("") == ""
    assert summarize_stream_json("not json\nat all") == "not json\nat all"
