"""The spec-to-code authoring loop, end to end with the agent mocked.

What this locks in:
  1. The scaffold's admin pages are REAL now (snapshot-rendered Service Charges, working
     Communications composer), not coming-soon stubs -- and both generated files pass a real
     php -l when PHP is present.
  2. refresh_scaffold upgrades an EXISTING source dir (the situation M is actually in: his dir
     was scaffolded from the old stub template and scaffold refuses non-empty dirs), with backup.
  3. author_plugin_change: dry-run by default; on apply, a good generation is kept and reported
     with changed files; a BAD generation (broken PHP) is rolled back byte-identically and
     reported as validation_failed. A bad generation can never sit in the source dir waiting to
     be built and deployed to the live site.
"""
from __future__ import annotations

import shutil
import warnings
from pathlib import Path


warnings.filterwarnings("ignore")


def _scaffolded(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "plugin-src"
    monkeypatch.setenv("MONGOOSE_PLUGIN_DIR", str(d))
    from mongoose.services.wp_plugin import scaffold_example_court_plugin
    r = scaffold_example_court_plugin("0.2.0")
    assert r["ok"], r
    return d


def test_scaffold_pages_are_real_not_stubs(tmp_path, monkeypatch):
    d = _scaffolded(tmp_path, monkeypatch)
    main = (d / "example-court-rtm.php").read_text()
    assert "coming soon" not in main
    assert "pc_rtm_snapshot" in main          # Service Charges renders the pushed snapshot
    assert "wp_insert_post" in main           # Communications actually creates notices
    assert "check_admin_referer( 'pc_rtm_notice' )" in main
    php = shutil.which("php")
    if php:
        import subprocess
        for f in (d / "example-court-rtm.php", d / "includes" / "portal-sync.php"):
            r = subprocess.run([php, "-l", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, r.stdout + r.stderr


def test_refresh_scaffold_upgrades_existing_dir_with_backup(tmp_path, monkeypatch):
    d = _scaffolded(tmp_path, monkeypatch)
    # Simulate the owner's real situation: source dir contains the OLD stub template.
    main = d / "example-court-rtm.php"
    main.write_text(main.read_text().replace("pc_rtm_snapshot", "OLD_TEMPLATE_MARKER"))
    (d / "my-custom-notes.txt").write_text("mine")  # a non-generated file must survive
    from mongoose.services.wp_plugin import refresh_scaffold
    r = refresh_scaffold("0.2.1")
    assert r["ok"], r
    assert "pc_rtm_snapshot" in main.read_text()          # template files regenerated
    assert (d / "my-custom-notes.txt").read_text() == "mine"  # custom file preserved
    assert Path(r["backup"]).exists()                      # backup of the old state kept


def test_author_dry_run_changes_nothing(tmp_path, monkeypatch):
    d = _scaffolded(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in d.rglob("*.php")}
    from mongoose.services.plugin_author import author_plugin_change
    r = author_plugin_change("Add an arrears summary box to the Service Charges page.")
    assert r["ok"] and r.get("dry_run") is True
    assert "task_preview" in r and "billing_note" in r
    assert {p: p.read_bytes() for p in d.rglob("*.php")} == before


def test_author_apply_keeps_a_valid_change(tmp_path, monkeypatch):
    d = _scaffolded(tmp_path, monkeypatch)
    def fake_invoke(task, repo):
        f = Path(repo) / "example-court-rtm.php"
        f.write_text(f.read_text().replace(
            "<h1>Service Charges</h1>", "<h1>Service Charges</h1><p>ARREARS BOX</p>"))
        return True, "edited 1 file", False
    import mongoose.services.builder as B
    monkeypatch.setattr(B, "_invoke_claude_code", fake_invoke)
    from mongoose.services.plugin_author import author_plugin_change
    r = author_plugin_change("Add an arrears summary box.", apply=True)
    assert r["ok"] and r["applied"] is True, r
    assert "example-court-rtm.php" in r["changed_files"]
    assert "ARREARS BOX" in (d / "example-court-rtm.php").read_text()
    assert "Nothing" in r["next"] and "shipped" in r["next"]  # authoring never deploys


def test_author_apply_rolls_back_a_broken_generation(tmp_path, monkeypatch):
    d = _scaffolded(tmp_path, monkeypatch)
    before = {str(p.relative_to(d)): p.read_bytes() for p in d.rglob("*.php")}
    def bad_invoke(task, repo):
        f = Path(repo) / "example-court-rtm.php"
        f.write_text("<?php function broken( { if (")   # unparseable
        return True, "edited 1 file", False
    import mongoose.services.builder as B
    monkeypatch.setattr(B, "_invoke_claude_code", bad_invoke)
    from mongoose.services.plugin_author import author_plugin_change
    r = author_plugin_change("Break everything please.", apply=True)
    assert r["ok"] is False and r["reason"] == "validation_failed" and r["restored"] is True
    after = {str(p.relative_to(d)): p.read_bytes() for p in d.rglob("*.php")}
    assert after == before, "rollback must restore the tree byte-identically"


def test_author_apply_reports_no_changes_honestly(tmp_path, monkeypatch):
    """The false-green M hit: Claude Code exits 0 but edits nothing, and the old code returned
    ok=True/'updated the source', so the build re-zipped the OLD files as if the spec were done.
    A zero-change run must now fail with reason 'no_changes' and leave the tree byte-identical."""
    d = _scaffolded(tmp_path, monkeypatch)
    before = {str(p.relative_to(d)): p.read_bytes() for p in d.rglob("*.php")}
    def noop_invoke(task, repo):
        return True, "I would add an arrears box (but made no edits)", False  # clean exit, no writes
    import mongoose.services.builder as B
    monkeypatch.setattr(B, "_invoke_claude_code", noop_invoke)
    from mongoose.services.plugin_author import author_plugin_change
    r = author_plugin_change("Add an arrears summary box.", apply=True)
    assert r["ok"] is False and r["applied"] is False and r["reason"] == "no_changes", r
    assert r.get("restored") is True and "output_tail" in r
    after = {str(p.relative_to(d)): p.read_bytes() for p in d.rglob("*.php")}
    assert after == before, "a no-change run must not disturb the tree"


def test_author_inlines_a_referenced_spec_file(tmp_path, monkeypatch):
    """'Implement RTM_DOCUMENT_INTAKE_SPEC.md' handed the agent a FILENAME, not requirements. The
    referenced spec file (when present in the plugin dir) must be inlined into the task prompt so a
    single headless pass actually has the requirements in front of it."""
    d = _scaffolded(tmp_path, monkeypatch)
    (d / "MY_SPEC.md").write_text("# Spec\nAdd a UNIQUE_SPEC_MARKER box to the dashboard.\n")
    seen = {}
    def capture_invoke(task, repo):
        seen["task"] = task
        return True, "no edits", False  # returns no_changes, but we only assert the prompt here
    import mongoose.services.builder as B
    monkeypatch.setattr(B, "_invoke_claude_code", capture_invoke)
    from mongoose.services.plugin_author import author_plugin_change
    author_plugin_change("Implement MY_SPEC.md", apply=True)
    assert "UNIQUE_SPEC_MARKER" in seen.get("task", ""), "referenced spec file was not inlined"
    assert "editing the files" in seen["task"].lower() or "EDITING the files" in seen["task"]


def test_author_gathers_the_queued_requirements_without_a_named_file(tmp_path, monkeypatch):
    """The owner must NOT have to remember/type a spec filename. A terse request must (a) gather the
    plugin's *_SPEC.md queue itself and hand it to the agent in priority order, and (b) not be rejected
    as too short just because it's terse, since the queued spec IS the substance to iterate."""
    d = _scaffolded(tmp_path, monkeypatch)
    (d / "RTM_DOCUMENT_INTAKE_SPEC.md").write_text("# Intake\nAdd a QUEUED_REQ_MARKER intake page.\n")
    seen = {}
    def capture_invoke(task, repo):
        seen["task"] = task
        return True, "no edits", False
    import mongoose.services.builder as B
    monkeypatch.setattr(B, "_invoke_claude_code", capture_invoke)
    from mongoose.services.plugin_author import author_plugin_change
    r = author_plugin_change("build RTM", apply=True)  # 9 chars, no filename named
    assert r.get("reason") != "spec_too_short", r
    assert "QUEUED_REQ_MARKER" in seen.get("task", ""), "queued *_SPEC.md was not gathered"
    assert "HIGHEST PRIORITY FIRST" in seen["task"]


def test_author_terse_request_with_nothing_queued_still_rejected(tmp_path, monkeypatch):
    _scaffolded(tmp_path, monkeypatch)  # a fresh scaffold has no *_SPEC.md / BUILD_BRIEF yet
    from mongoose.services.plugin_author import author_plugin_change
    assert author_plugin_change("build RTM", apply=True)["reason"] == "spec_too_short"


def test_write_capable_invokers_do_not_use_the_deny_writes_mode():
    """The false-green's true root cause: `--permission-mode dontAsk` DENIES every Write/Edit headless,
    so Claude Code read files, made zero edits, and still exited 0. Every invoker that is supposed to
    CHANGE files must use acceptEdits (or bypassPermissions), never dontAsk. Read-only reviewers keep
    plan/dontAsk. Pin this so an edit can't silently reintroduce the write-denying mode."""
    import inspect
    from mongoose.services import builder, self_patch, remote_build
    # builder invoker: the ACTUAL command list (not the docstring, which names dontAsk to explain the
    # bug) must pass acceptEdits.
    b = inspect.getsource(builder._invoke_claude_code)
    cmd_region = b.split('"""', 2)[-1]  # everything after the docstring
    assert '"acceptEdits"' in cmd_region and '"dontAsk"' not in cmd_region, \
        "builder invoker must author with acceptEdits, not the deny-writes dontAsk mode"
    # remote_build author command uses acceptEdits, not the deny-writes mode
    rb = inspect.getsource(remote_build)
    assert "permission-mode acceptEdits" in rb and "permission-mode dontAsk" not in rb, \
        "remote_build author must use acceptEdits"
    # self_patch: the AUTHOR call uses acceptEdits; the read-only peer reviewer stays 'plan'
    sp = inspect.getsource(self_patch)
    assert '"acceptEdits"' in sp and '"plan"' in sp and '"dontAsk"' not in sp, \
        "self_patch author must use acceptEdits (reviewer stays plan)"


def test_author_refuses_empty_spec_and_missing_source(tmp_path, monkeypatch):
    from mongoose.services.plugin_author import author_plugin_change
    # missing source wins first: without a plugin there is nothing to author, terse or not
    monkeypatch.setenv("MONGOOSE_PLUGIN_DIR", str(tmp_path / "nowhere"))
    assert author_plugin_change("hi")["reason"] == "no_source"
    assert author_plugin_change("Add a page that lists notices.")["reason"] == "no_source"
    # with a real scaffold but a contentless request AND nothing queued: too short
    _scaffolded(tmp_path, monkeypatch)
    assert author_plugin_change("hi")["reason"] == "spec_too_short"


def test_author_rolls_back_broken_generation_even_without_php(tmp_path, monkeypatch):
    """macOS no longer bundles PHP, so on the real deployment Mac php -l is typically absent.
    The structural fallback must still catch a garbled generation and roll back."""
    d = _scaffolded(tmp_path, monkeypatch)
    before = {str(p.relative_to(d)): p.read_bytes() for p in d.rglob("*.php")}
    def bad_invoke(task, repo):
        f = Path(repo) / "example-court-rtm.php"
        f.write_text("<?php function broken( { if (")
        return True, "edited", False
    import mongoose.services.builder as B
    import mongoose.services.plugin_author as PA
    monkeypatch.setattr(B, "_invoke_claude_code", bad_invoke)
    monkeypatch.setattr(PA.shutil, "which", lambda name: None)  # simulate a Mac without php
    r = PA.author_plugin_change("Rewrite the charges page completely.", apply=True)
    assert r["ok"] is False and r["reason"] == "validation_failed" and r["restored"] is True
    assert any("unbalanced" in pr for pr in r["problems"])
    after = {str(p.relative_to(d)): p.read_bytes() for p in d.rglob("*.php")}
    assert after == before
