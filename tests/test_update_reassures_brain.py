"""Every update must reassure the user that the brain is preserved , at the preflight summary and at the
post-update confirmation. We assert the reassurance strings are present in the updater source so they
cannot be quietly dropped."""
from __future__ import annotations

from pathlib import Path


def _updater_src() -> str:
    import mongoose.updater as U
    return Path(U.__file__).read_text()


def test_preflight_mentions_brain_preserved():
    src = _updater_src()
    assert "is preserved across the update" in src
    assert "home folder" in src


def test_snapshot_block_reassures_brain_is_safe():
    src = _updater_src()
    assert "Your brain is safe." in src
    assert "OUTSIDE this install" in src
    assert "rollback point" in src


def test_post_update_confirms_brain_intact():
    src = _updater_src()
    assert "carried across unharmed" in src
