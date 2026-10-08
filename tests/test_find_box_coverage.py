"""Guard for cockpit find-box ('find a menu, section or function') coverage.

Root cause of the reported gap: the find-box index is a hand-curated keyword list decoupled from the
actual controls, so a function could exist with no way to search for it (that is how password reset
slipped). This test locks in that the KEY user-facing functions are searchable, and , crucially for the
chat-driven ones , that each find-box chat DRAFT actually routes to a real handler via the intent
resolver, so a 'found' function can never dead-end in the model instead of doing the thing."""
import re

import pytest

SRC = open("src/mongoose/cockpit.py", encoding="utf-8").read()
_KW_BLOB = " ".join(re.findall(r'kw:"([^"]+)"', SRC)).lower()


@pytest.mark.parametrize("term", [
    "reset password", "repair", "pair", "guest invite", "arrears", "build rtm plugin",
    "protocol v3", "tailscale", "music", "prepare", "surprise", "webhook",
])
def test_key_functions_are_searchable(term):
    # every listed term (or its lead word) must appear somewhere in the find-box keyword index
    assert term in _KW_BLOB or term.split()[0] in _KW_BLOB, f"'{term}' is not findable in the cockpit find box"


# Each chat-driven find entry opens chat prepopulated with this draft; the draft MUST route to the
# named handler (not fall through to the model), or the found function silently does nothing useful.
CHAT_DRAFTS = {
    "Give me a guest invite link for ": "guest_invite",
    "Prepare me for ": "prepare",
    "Surprise me": "suggestion",
    "How are the arrears looking?": "rtm_operational",
    "Build the next RTM plugin iteration": "rtm_plugin",
    "Play some music": "home_control",
}


@pytest.mark.parametrize("draft,intent", list(CHAT_DRAFTS.items()))
def test_find_box_chat_draft_is_present_and_routes(draft, intent):
    assert f'chat:"{draft}"' in SRC, f"find-box chat draft not present verbatim: {draft!r}"
    from mongoose.services.intent_resolver import resolve
    assert resolve(draft)[0] == intent, f"draft {draft!r} routes to {resolve(draft)[0]}, expected {intent}"


# ── Launchpad (owner's daily phone surface) find box ────────────────────────────────────────────
# The launchpad previously had NO find box at all, so a function was unreachable from the surface M
# actually lives in. It now has one (Status pane). Its SCREEN results deep-link to /device/cockpit,
# its CHAT results prefill the launchpad's own chat. These tests stop that box drifting from reality:
# a screen view must be a real cockpit pane reachable via the deep-link, and a chat draft must route.
LAUNCHPAD_CHAT_DRAFTS = {
    "reset Sam’s password": "password_reset",
    "Give me a guest invite link for ": "guest_invite",
    "How are the arrears looking?": "rtm_operational",
    "Build the next RTM plugin iteration": "rtm_plugin",
    "Play some music": "home_control",
    "Prepare me for ": "prepare",
    "Surprise me": "suggestion",
}
LAUNCHPAD_SCREEN_VIEWS = ["approvals", "scheduler", "devices", "activity", "search",
                          "setup", "whatsapp", "rtm", "build", "system"]


def test_launchpad_has_a_find_box():
    assert 'id="lp-find"' in SRC, "launchpad (owner daily surface) lost its find box input"
    assert "function lpFindInit(" in SRC and "_lpIndex=lpFindIndex()" in SRC


def test_launchpad_screen_links_reach_a_real_cockpit_pane():
    # the deep-link only works if phone_cockpit honours #view= AND the target pane exists
    assert "function openHashView(" in SRC, "phone_cockpit dropped #view= deep-link support"
    for v in LAUNCHPAD_SCREEN_VIEWS:
        assert f'id="v-{v}"' in SRC, f"launchpad find links to cockpit view {v!r} with no v-{v} pane"


@pytest.mark.parametrize("draft,intent", list(LAUNCHPAD_CHAT_DRAFTS.items()))
def test_launchpad_chat_draft_is_present_and_routes(draft, intent):
    assert draft in SRC, f"launchpad find chat draft not present verbatim: {draft!r}"
    from mongoose.services.intent_resolver import resolve
    assert resolve(draft)[0] == intent, f"launchpad draft {draft!r} routes to {resolve(draft)[0]}, expected {intent}"
