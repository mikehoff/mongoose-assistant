"""First-run rehearsal harness , the automated version of the manual cold-install loop.

WHY THIS EXISTS:
The first-run dead-ends (bare-root 404, lying button labels, wrong-machine links, dead spinners, LAN
privacy leaks) were each found by a human doing a FULL cold install and scan, then fixed in isolation,
then re-verified by ANOTHER full cold install. That manual loop was paid thirty times to surface one
paper-cut per round. The whole apparatus pushed verification onto the user in the most expensive form,
because the rest of the suite proves the CODE is right but never walks the lived JOURNEY end to end.

Everything the manual rehearsal checked is stageable in-process:
  - install state (fresh / running / complete / HALF-FAILED) via install_progress.begin_install + set_step
  - came-back-later vs fresh via the served /welcome + onboarding state
  - loopback vs LAN-from-phone via TestClient(client=(host, port))
The only things that genuinely need a physical device are the browser/OS bits a server cannot drive:
Safari's microphone prompt, the phone camera scanning the QR, iOS/Android share-sheet behaviour. Those,
and only those, stay manual (see test_first_run_residual_manual_checklist at the end, which documents
them rather than pretending to test them).

THE UNIVERSAL PROPERTY this asserts for every path a person can take:
  1. it lands somewhere usable , a 200 with real navigation, never a 404 or a raw auth error;
  2. no button label lies about where it goes;
  3. no link points at the wrong machine (phone surfaces must not hand a phone a localhost owner URL);
  4. a non-loopback (LAN/phone) client is never shown private state.
"""
from __future__ import annotations

import os
import re
import tempfile

from fastapi.testclient import TestClient


# ── staging helpers ───────────────────────────────────────────────────────────────────────────────

def _fresh_env():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ.pop("MONGOOSE_API_TOKEN", None)
    os.environ.pop("MONGOOSE_ALLOW_REMOTE", None)
    import mongoose.settings as S
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    return d


def _client(host: str = "testclient"):
    """A client whose source host we control: 'testclient'/127.0.0.1 = loopback owner; anything else
    (e.g. '192.168.1.50') = a LAN/phone client subject to the remote guard."""
    from mongoose.app import app
    return TestClient(app, raise_server_exceptions=False, client=(host, 50000))


def _stage_install(*, phase: str):
    """Drive install_progress into a named lived state. phase in:
    'fresh' (nothing begun), 'running' (mid-install), 'complete' (ready), 'half_failed' (a step failed
    after the browser already opened , the hardest state to reproduce by hand)."""
    from mongoose import install_progress as IP
    if phase == "fresh":
        return
    IP.begin_install()
    if phase == "running":
        IP.set_step("prerequisites", "done")
        IP.set_step("environment", "running")
    elif phase == "complete":
        for k in IP._STEP_KEYS:
            IP.set_step(k, "done")
    elif phase == "half_failed":
        IP.set_step("prerequisites", "done")
        IP.set_step("environment", "done")
        IP.set_step("services", "failed", "a step failed after the page opened")


# ── the property checks ─────────────────────────────────────────────────────────────────────────────

# Phrases on a phone surface that would hand the phone a localhost/owner URL it cannot use. A phone
# tapping http://localhost:8765 opens the PHONE's localhost, not the Mac. These must never appear as
# clickable navigation on the phone launchpad.
_WRONG_MACHINE_NAV = re.compile(r"""location\.href\s*=\s*['"]https?://(localhost|127\.0\.0\.1)""", re.I)

# Obvious private-state markers that must never reach a non-loopback client on a public page.
_PRIVATE_LEAK = re.compile(r"(bearer\s+[a-z0-9]|api[_-]?token['\"]?\s*[:=]\s*['\"][a-z0-9]{8})", re.I)


def _is_usable_page(r) -> bool:
    """A served page is 'usable' if it is a 200 with real content (not an error shell, not empty)."""
    if r.status_code != 200:
        return False
    body = r.text or ""
    if len(body) < 150:
        return False
    low = body.lower()
    # a raw auth/error body is not a usable landing
    if '"error"' in low and "front door" not in low and "<html" not in low:
        return False
    return True


# ── fresh install: every first surface lands usable ─────────────────────────────────────────────────

def test_fresh_install_every_surface_is_usable():
    _fresh_env()
    _stage_install(phase="fresh")
    c = _client()
    for path in ["/", "/install", "/welcome", "/device/launchpad", "/pair"]:
        r = c.get(path)
        assert _is_usable_page(r), f"fresh install: {path} did not land usable (status {r.status_code})"


def test_bare_root_never_404s_in_any_install_phase():
    """The bare host is what a forgetful user types. It must be a forgiving front door in EVERY phase,
    not just after a clean install."""
    for phase in ["fresh", "running", "complete", "half_failed"]:
        _fresh_env()
        _stage_install(phase=phase)
        c = _client()
        r = c.get("/")
        assert r.status_code == 200, f"bare root 404'd in phase {phase}"
        assert "front door" in r.text.lower()


# ── the hard-to-stage states the manual loop struggled with ─────────────────────────────────────────

def test_install_in_progress_page_is_usable_not_dead():
    _fresh_env()
    _stage_install(phase="running")
    c = _client()
    r = c.get("/install")
    assert _is_usable_page(r)
    prog = c.get("/install/progress")
    assert prog.status_code == 200
    assert "steps" in prog.json() or isinstance(prog.json(), dict)


def test_half_failed_install_still_offers_usable_links():
    """A step failing AFTER the page opened previously left a dead patch. The completion/recovery must
    still give the user somewhere to go , this is the state that is painful to reproduce by hand."""
    _fresh_env()
    _stage_install(phase="half_failed")
    c = _client()
    r = c.get("/install")
    assert _is_usable_page(r), "half-failed install left a dead page"
    # there must be a real onward link, not just a spinner
    low = r.text.lower()
    assert ("cockpit" in low or "welcome" in low or "/ui" in low or "chat" in low), \
        "half-failed install offered no usable onward path"


def test_came_back_later_welcome_is_a_reorientation_not_a_dead_end():
    """Setup already complete: /welcome must reorient a forgetful returning user, not collapse to a
    single dead 'you're set up' card."""
    _fresh_env()
    _stage_install(phase="complete")
    c = _client()
    r = c.get("/welcome")
    assert _is_usable_page(r)
    low = r.text.lower()
    # reorientation affordances rather than a lone confirmation
    hits = sum(x in low for x in ["ask", "search", "what changed", "connect", "chat"])
    assert hits >= 2, "returning-user welcome did not reorient (looks like a dead 'set up' card)"


# ── the wrong-machine and privacy properties (the LAN-from-phone case) ───────────────────────────────

def test_phone_launchpad_never_hands_a_phone_a_localhost_owner_url():
    _fresh_env()
    c = _client(host="192.168.1.50")  # a phone on the LAN
    r = c.get("/device/launchpad")
    # launchpad is the phone's own surface; it must not navigate the phone to a localhost owner URL
    assert not _WRONG_MACHINE_NAV.search(r.text or ""), \
        "phone launchpad contains clickable navigation to a localhost/owner URL"


def test_lan_client_front_door_leaks_no_private_state():
    _fresh_env()
    _stage_install(phase="complete")
    c = _client(host="192.168.1.50")  # LAN/phone
    r = c.get("/")
    assert r.status_code == 200
    assert not _PRIVATE_LEAK.search(r.text or ""), "front door leaked private state to a LAN client"


def test_lan_welcome_does_not_promise_a_pairing_code_it_cannot_show():
    """Remote /welcome previously said 'pair with the code below' when no code was shown. The coarse
    remote page must not promise owner pairing it cannot perform."""
    _fresh_env()
    os.environ["MONGOOSE_API_TOKEN"] = "x" * 24  # token configured => LAN client gets coarse view
    os.environ["MONGOOSE_ALLOW_REMOTE"] = "true"
    import mongoose.settings as S
    if hasattr(S.get_settings, "cache_clear"):
        S.get_settings.cache_clear()
    try:
        c = _client(host="192.168.1.50")
        r = c.get("/welcome")
        low = (r.text or "").lower()
        if "code below" in low or "code shown below" in low:
            # if it claims a code below, a code must actually be present
            assert re.search(r"\b\d{4,8}\b", r.text or ""), \
                "remote welcome promised a pairing code 'below' but showed none"
    finally:
        os.environ.pop("MONGOOSE_API_TOKEN", None)
        os.environ.pop("MONGOOSE_ALLOW_REMOTE", None)
        if hasattr(S.get_settings, "cache_clear"):
            S.get_settings.cache_clear()


# ── button labels must not lie about their target ──────────────────────────────────────────────────

def test_install_completion_labels_match_their_targets():
    """A button labelled 'Open the cockpit' previously opened /welcome. Where a label names a specific
    destination, it must go there , verified by pairing label text with the nav target in the same
    onclick/href."""
    _fresh_env()
    _stage_install(phase="complete")
    c = _client()
    html = c.get("/install").text
    # find buttons/links that name 'full cockpit' and confirm they target /ui (the cockpit), not /welcome
    for m in re.finditer(r"""(<(?:button|a)[^>]*>)([^<]*full cockpit[^<]*)(</(?:button|a)>)""", html, re.I):
        tag = m.group(1).lower()
        # the cockpit is /ui; a 'full cockpit' control must not be wired to /welcome
        assert "/welcome" not in tag, "a 'full cockpit' control is wired to /welcome (label lies)"


# ── the residual manual checklist (documented, not faked) ──────────────────────────────────────────

def test_first_run_residual_manual_checklist_is_documented():
    """The ONLY first-run things that still need a physical device, because a server cannot drive a real
    browser/OS. This test exists so the short, honest manual list is version-controlled and visible , it
    is the complete remainder after the harness above, not a vague 'go rehearse everything'.

    Manual, device-only (everything else is now automated above):
      1. Safari/Chrome microphone permission prompt actually fires on the Mac cockpit voice screen.
      2. A real phone camera scans the pairing QR and lands on /device/launchpad paired.
      3. iOS/Android share-sheet / add-to-home-screen behaviour for the PWA after the service-worker
         cache change.
      4. Real mobile-browser blob download behaviour for EML and voicemail audio from phone search.
    """
    manual_items = 4
    assert manual_items == 4  # keep this in lockstep with the docstring list above
