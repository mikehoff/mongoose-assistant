"""Active-layout reachability gate.

This codebase carries two generations of cockpit layout in one JS blob, so a later duplicate function
declaration silently shadows an earlier one (last declaration wins). That is exactly how the v1.11-1.13
System surfaces went dead while every API test stayed green. This gate freezes the duplicate set and
asserts the WINNING (last) definitions actually carry the live features."""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "mongoose" / "cockpit.py"

# Legacy duplicates that predate this gate (two cockpit generations share utility names and loaders).
# The set is FROZEN: anything NEW appearing here means a feature just went dead by shadowing.
ALLOWED_DUPLICATES = {"loadSystem", "loadSetup", "loadScheduler", "loadMusic", "loadDevices",
                      "loadBuild", "loadApprovals", "loadActivity",
                      "api", "approve", "boot", "deployStage", "esc", "firePoke", "go",
                      "installMissing", "j", "localOwnerBase", "localUiUrl", "postAction",
                      "runDoctor", "token", "viewLog", "waConnect",
                      # System manage panels are deliberately defined in BOTH documents (desktop page
                      # and PHONE_COCKPIT_HTML); each page needs its own copy in scope.
                      "dumpOpen", "dumpLoadList", "dumpCreate", "suFetch", "suLoadStages",
                      "suCheck", "suDrill", "suApprove", "suggLoad", "suggFb", "renderSystemManagePanels", "loadKnowledge",
                      # Phone-repair polling helper needed in three self-contained documents
                      # (cockpit_html, WELCOME_HTML, INSTALL_HTML) that share no JS between
                      # them; each defines its own copy in its own scope, same pattern as
                      # localOwnerBase/localUiUrl above.
                      "waitForMongooseThenReload"}


def _defs(text: str) -> list[tuple[str, int]]:
    return [(m.group(1), m.start()) for m in
            re.finditer(r"^(?:async )?function (\w+)\s*\(", text, re.M)]


def _body_of_last(text: str, name: str) -> str:
    starts = [pos for n, pos in _defs(text) if n == name]
    assert starts, f"{name} is not defined at all"
    start = starts[-1]
    nxt = [pos for n, pos in _defs(text) if pos > start]
    return text[start:(nxt[0] if nxt else len(text))]


def test_no_new_shadowing_duplicates():
    text = SRC.read_text()
    names = [n for n, _ in _defs(text)]
    dupes = {n for n in names if names.count(n) > 1}
    new = dupes - ALLOWED_DUPLICATES
    assert not new, f"NEW duplicate function declarations shadow earlier ones: {sorted(new)}"


def test_winning_loadsystem_hosts_manage_panels_and_knowledge():
    text = SRC.read_text()
    body = _body_of_last(text, "loadSystem")
    assert "renderSystemManagePanels" in body, "winning loadSystem does not mount the manage panels"
    panels = _body_of_last(text, "renderSystemManagePanels")
    for marker in ("dumpCreate", "suFetch", "sugg-list", "sys-knowledge", "loadKnowledge"):
        assert marker in panels, f"manage panels missing '{marker}'"
    # the panel renderer exists per document (launchpad + phone); desktop mounts inline HTML.
    names = [n for n, _ in _defs(text)]
    assert 1 <= names.count("renderSystemManagePanels") <= 3


def test_dump_downloads_never_use_headerless_anchor():
    text = SRC.read_text()
    assert 'href="/dump/' not in text, "anchor download cannot carry the nonce header; use dumpOpen()"
    assert "async function dumpOpen(" in text
