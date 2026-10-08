"""The setup flow copy should be warm and inviting, not clinical , and the progress affordances present.
These guard against regressing the 'delightful setup' work back to flat functional text."""
from mongoose.cockpit import COCKPIT_HTML
from mongoose.wizard import WIZARD_STAGES


def test_stage_titles_are_warm_not_clinical():
    titles = {s["title"] for s in WIZARD_STAGES}
    # the old clinical titles must be gone
    assert "Your data" not in titles
    assert "Reach you & run on its own" not in titles
    # the warm ones present
    assert "Your world" in titles
    assert "Stay in touch" in titles


def test_stage_blurbs_reassure_skippability_warmly():
    blurbs = " ".join(s["blurb"] for s in WIZARD_STAGES).lower()
    # warmth + permission to skip, phrased kindly
    assert "optional" in blurbs or "skip" in blurbs or "none" in blurbs


def test_setup_header_has_progress_and_warm_headlines():
    js = COCKPIT_HTML
    # progress affordances
    assert "setup-progress-bar" in js
    assert "setup-progress-headline" in js
    assert "refreshSetupProgress" in js
    # warm headlines for the journey
    assert "Let's make Mongoose yours" in js
    assert "You're in great shape" in js


def test_core_not_ready_is_encouraging_not_alarming():
    js = COCKPIT_HTML
    assert "Almost there" in js
    # the harsh old phrasing should be gone from the header
    assert "Local core not ready" not in js


def test_save_success_celebrates():
    js = COCKPIT_HTML
    assert "celebrateSetup" in js


def test_install_arrival_is_warm():
    from mongoose.cockpit import INSTALL_HTML
    assert "and it's yours" in INSTALL_HTML
