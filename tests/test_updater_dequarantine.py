"""The updater and rollback paths must de-quarantine the install dir.

Root cause of the reported "sometimes the update just works, sometimes macOS makes me re-approve
in System Settings" behaviour: an update zip downloaded by Safari/Mail/AirDrop carries
com.apple.quarantine on every file; cp/rsync preserve it; the .command wrappers then trip
Gatekeeper again. The installer already strips the flag, so the fix is to make the updater and
the rollback path do the same, giving a consistent trusted-tree outcome regardless of how the
zip arrived.
"""
from __future__ import annotations

import pathlib
import re

UPDATE_SH = pathlib.Path("scripts/update.sh").read_text()


def test_updater_dequarantines_after_copying_new_files():
    # After the file-copy loop that lays down the new version, there must be an xattr strip.
    copy_marker = 'echo "Preserved: .env, data/, secrets/'
    assert copy_marker in UPDATE_SH
    after = UPDATE_SH.split(copy_marker, 1)[1]
    assert "xattr -dr com.apple.quarantine ." in after, \
        "updater must strip com.apple.quarantine after copying new files, or macOS re-prompts"


def test_rollback_dequarantines_after_restore():
    restore_marker = "Restore code + state from backup"
    assert restore_marker in UPDATE_SH
    after = UPDATE_SH.split(restore_marker, 1)[1]
    window = after[:400]
    assert "xattr -dr com.apple.quarantine ." in window, \
        "rollback must also strip quarantine so a restored tree stays trusted"


def test_dequarantine_is_guarded_by_command_check():
    # Must not hard-fail on systems without xattr (non-macOS dev/CI).
    for m in re.finditer(r"xattr -dr com\.apple\.quarantine", UPDATE_SH):
        preceding = UPDATE_SH[:m.start()]
        # the nearest guard above should be a command -v xattr check
        assert "command -v xattr" in preceding.rsplit("\n\n", 1)[-1] or \
               "command -v xattr" in preceding[-400:], \
            "each xattr call must be guarded by 'command -v xattr' so non-macOS runs are no-ops"
