"""Plugin authoring: turn a written spec into real changes to the WordPress plugin source.

This is the wire that was always missing between "tell Mongoose chat what the plugin should do"
and code actually changing. Everything else already existed: builder._invoke_claude_code runs a
headless `claude -p` against a repo; claude_code.recommend_route knows whether local or the Pi
is the right executor (and that headless runs bill separately from the flat plan); wp_plugin
builds and deploys whatever is in the plugin SOURCE DIRECTORY. None of it pointed at the plugin.
This module points it there, with the guardrails a live legal/money site needs:

  - DRY-RUN BY DEFAULT: author_plugin_change(spec) returns the exact task that WOULD run, the
    chosen route, and the cost caveat. Nothing changes until apply=True.
  - BACKUP + ROLLBACK: the source dir is copied aside before the agent touches it; if validation
    fails afterwards, the tree is restored and the failure reported honestly.
  - VALIDATION: `php -l` on every .php file when PHP is installed; a structural check (balanced
    php tags, plugin header intact, no truncation sentinel) always. A change that fails
    validation NEVER stays in the source dir, so a bad generation can't be built or deployed.
  - SCOPE FENCE: the task prompt confines the agent to the plugin directory and forbids touching
    secrets, weakening the nonce/HMAC patterns, or removing the plugin header.

Deploying still goes through the existing, separate build -> deploy verbs. Authoring never
ships anything by itself.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import profile as _profile
from .wp_plugin import _plugin_dir

_TRUNCATION_SENTINEL = "..."  # a bare trailing ellipsis at EOF is the classic cut-off LLM output


def _main_file_name() -> str:
    """The plugin's main file, <plugin_slug>.php, from the site profile."""
    return f"{_profile.wp_plugin_slug()}.php"


def _task_prompt(spec: str, plugin_dir: Path) -> str:
    return (
        f"You are editing the {_profile.rtm_name()} RTM WordPress plugin. Work ONLY inside this "
        f"directory: {plugin_dir}\n\n"
        "Hard rules:\n"
        "  1. Never touch any file outside that directory.\n"
        f"  2. Keep the plugin header comment in {_main_file_name()} intact (Plugin Name, "
        "Version, etc.).\n"
        "  3. Keep every existing security pattern: check_admin_referer / wp_nonce_field on "
        "forms, pc_rtm_verify_hmac on REST routes, esc_html/esc_attr/esc_url on output, "
        "sanitize_* on input, manage_options permission checks on admin pages.\n"
        "  4. Never write secrets, tokens, or credentials into the code.\n"
        "  5. PHP must parse: no unclosed braces, strings, or tags.\n"
        "  6. Prefer small, surgical edits over rewrites; preserve working behaviour.\n\n"
        "The change requested by the owner (implement it fully):\n"
        f"{spec.strip()}\n\n"
        "IMPORTANT: implement this by EDITING the files in that directory directly with your "
        "file-editing tools right now. Do not merely describe a plan or summarise what should be "
        "done: this run is judged solely by the file changes it makes. If nothing needs changing, "
        "say so explicitly, but otherwise make the edits before you finish.\n"
    )


def _php_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.php") if p.is_file())


def _requirements_context(plugin_dir: Path, *, named: str | None = None, cap: int = 20000) -> str:
    """Gather the plugin's queued requirements so the owner never has to remember or type a spec
    filename. Priority order (highest first): a doc the owner explicitly named this turn, then the
    formal SPEC / REQUIREMENTS markdown files (alphabetical, deterministic), then the accumulated
    BUILD_BRIEF request log. Concatenated with headers, size-capped. Returns "" if the dir has none.
    """
    docs: list[Path] = []
    def _add(p: Path) -> None:
        if p.exists() and p.is_file() and p not in docs:
            docs.append(p)
    if named:
        _add(plugin_dir / named)
    formal = [p for p in sorted(plugin_dir.glob("*.md"))
              if any(k in p.name.upper() for k in ("SPEC", "REQUIREMENT"))]
    for p in formal:
        _add(p)
    _add(plugin_dir / "BUILD_BRIEF.md")  # lowest priority: the running request log
    if not docs:
        return ""
    parts, used = ["Queued requirements to implement, HIGHEST PRIORITY FIRST. Read the current source, "
                   "then implement the highest-priority requirement that the code does not yet satisfy "
                   "(and as many further ones as you cleanly can). These are the queue, in order:"], 0
    for p in docs:
        body = p.read_text(encoding="utf-8", errors="replace")
        remaining = cap - used
        if remaining <= 200:
            break
        chunk = body[:remaining]
        parts.append(f"\n===== {p.name} =====\n{chunk}")
        used += len(chunk)
    return "\n".join(parts)


def _validate_tree(root: Path) -> dict[str, Any]:
    """php -l every file when PHP exists; structural checks always. Returns {ok, problems}."""
    problems: list[str] = []
    files = _php_files(root)
    if not files:
        return {"ok": False, "problems": ["no .php files found after the edit"]}
    main = root / _main_file_name()
    if main.exists():
        head = main.read_text(encoding="utf-8", errors="replace")[:600]
        if "Plugin Name:" not in head:
            problems.append(f"plugin header (Plugin Name:) missing from {main.name}")
    else:
        problems.append(f"{main.name} missing")
    php = shutil.which("php")
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        if text.rstrip().endswith(_TRUNCATION_SENTINEL):
            problems.append(f"{f.name}: ends with a bare ellipsis (looks like truncated output)")
        if text.count("<?php") == 0:
            problems.append(f"{f.name}: no opening <?php tag")
        if php:
            r = subprocess.run([php, "-l", str(f)], capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                problems.append(f"{f.name}: {(r.stdout or r.stderr).strip()[:300]}")
        else:
            # macOS stopped bundling PHP, so on the actual deployment Mac `php -l` is usually
            # absent. This crude balance check is no substitute for a parser, but it catches the
            # dominant failure mode of a cut-off or garbled generation: unbalanced braces/parens
            # outside strings and comments.
            depth_b = depth_p = 0
            in_s = in_d = in_lc = in_bc = False
            prev = ""
            for ch in text:
                if in_lc:
                    if ch == "\n": in_lc = False
                elif in_bc:
                    if prev == "*" and ch == "/": in_bc = False
                elif in_s:
                    if ch == "'" and prev != "\\": in_s = False
                elif in_d:
                    if ch == '"' and prev != "\\": in_d = False
                else:
                    if ch == "'": in_s = True
                    elif ch == '"': in_d = True
                    elif prev == "/" and ch == "/": in_lc = True
                    elif prev == "/" and ch == "*": in_bc = True
                    elif ch == "{": depth_b += 1
                    elif ch == "}": depth_b -= 1
                    elif ch == "(": depth_p += 1
                    elif ch == ")": depth_p -= 1
                prev = ch
            if depth_b != 0 or depth_p != 0:
                problems.append(f"{f.name}: unbalanced braces/parens "
                                f"(braces {depth_b:+d}, parens {depth_p:+d}); install PHP "
                                "(brew install php) for full syntax checking")
    return {"ok": not problems, "problems": problems, "php_lint": bool(php),
            "files_checked": len(files),
            "note": ("" if php else "php not installed: structural checks only. One-time "
                     "`brew install php` on the Mac enables real syntax validation.")}


def author_plugin_change(spec: str, *, route: str = "auto", apply: bool = False,
                         timeout: int = 600) -> dict[str, Any]:
    """The chat-facing entry point. spec = plain English of what the plugin should do.

    apply=False (default): returns the plan (task text, route, caveats) without running anything.
    apply=True: backup -> run claude code on the plugin dir -> validate -> keep or roll back.
    """
    spec = (spec or "").strip()
    plugin_dir = _plugin_dir()
    if not plugin_dir.exists() or not any(plugin_dir.iterdir()):
        return {"ok": False, "reason": "no_source", "path": str(plugin_dir),
                "hint": "Scaffold the plugin first (wp_plugin_action scaffold)."}
    # Safety guard (adopted from the parallel coder's patch): never point the code agent at a
    # directory that isn't this site's RTM plugin. MONGOOSE_PLUGIN_DIR aimed at the wrong
    # folder must be a refusal, not a backup-then-edit of something unrelated.
    rtm_name = _profile.rtm_name()
    main = plugin_dir / _main_file_name()
    if not (main.exists() and rtm_name in main.read_text(encoding="utf-8",
                                                         errors="replace")[:800]):
        return {"ok": False, "reason": f"unsafe_non_{_profile.rtm_slug().replace('-', '_')}_source",
                "path": str(plugin_dir),
                "hint": f"MONGOOSE_PLUGIN_DIR is not the {rtm_name} RTM plugin source. Fix the "
                        "path (or scaffold) before authoring from chat."}

    # The owner must NOT have to remember or type an exact spec filename. Whatever they said ("build
    # the RTM plugin", "implement the spec", "keep building"), gather the plugin's queued requirement
    # docs and hand the agent the whole queue in priority order. If they DID name a doc, it goes first.
    import re as _re
    _ref = _re.search(r"([A-Za-z0-9_\-]+\.md)", spec)
    _named = _ref.group(1) if _ref else None
    _ctx = _requirements_context(plugin_dir, named=_named)
    # A terse request ("build RTM") is fine WHEN there is a queued spec to iterate; it is only too
    # short if there is also nothing queued to work from.
    if len(spec) < 12 and not _ctx:
        return {"ok": False, "reason": "spec_too_short",
                "hint": "Describe the change in a sentence, or add a *_SPEC.md to the plugin folder "
                        "so 'build the RTM plugin' can iterate the queued requirements automatically."}
    if _ctx:
        spec = f"{spec}\n\n{_ctx}"

    from .claude_code import recommend_route
    rec = recommend_route("code")
    chosen = route if route in ("local", "pi") else str(rec.get("route") or "local")
    task = _task_prompt(spec, plugin_dir)

    plan = {
        "spec": spec,
        "target": str(plugin_dir),
        "route": chosen,
        "route_reason": rec.get("why") or rec.get("reason") or "",
        "billing_note": ("Headless `claude -p` runs bill via the API key, separately from the "
                         "flat Claude plan. This is one bounded run per change."),
        "task_preview": task[:800],
    }
    if not apply:
        plan.update({"ok": True, "dry_run": True,
                     "next": "Re-run with apply=true to make the change."})
        return plan

    if chosen == "pi":
        # The plugin source lives on this Mac; the Pi offload path targets a repo already ON the
        # Pi. Syncing the plugin dir over and back is planned but not wired, and pretending
        # otherwise would be a fake-green. Fall back honestly.
        plan["route"] = "local"
        plan["route_note"] = ("Pi offload for the plugin isn't wired yet (source lives on this "
                              "Mac); ran locally instead.")

    backup = plugin_dir.parent / (
        f"{plugin_dir.name}-author-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    try:
        shutil.copytree(plugin_dir, backup)
    except Exception as exc:
        return {"ok": False, "reason": f"backup_failed: {exc}"}

    # Build-brief provenance (adopted idea): the owner's own words live in the source tree, so
    # the agent sees the accumulated intent and any human reading the plugin later knows why it
    # is shaped the way it is.
    from datetime import datetime as _dt2
    brief = plugin_dir / "BUILD_BRIEF.md"
    stamp = _dt2.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n## {stamp}\n\n{spec}\n"
    if brief.exists():
        brief.write_text(brief.read_text(encoding="utf-8") + entry, encoding="utf-8")
    else:
        brief.write_text(f"# {_profile.rtm_name()} plugin: build briefs (owner requests, newest last)\n"
                         + entry, encoding="utf-8")

    before = {str(f.relative_to(plugin_dir)): f.stat().st_mtime for f in _php_files(plugin_dir)}
    try:
        from .builder import _invoke_claude_code
        ok, output, rate_limited = _invoke_claude_code(task, plugin_dir)
    except Exception as exc:
        ok, output, rate_limited = False, f"invoker error: {exc}", False

    if not ok:
        # Nothing may have been written, but restore anyway so the tree is exactly pre-run.
        shutil.rmtree(plugin_dir, ignore_errors=True)
        shutil.move(str(backup), str(plugin_dir))
        return {**plan, "ok": False, "applied": False, "rate_limited": rate_limited,
                "reason": "agent_run_failed", "output_tail": (output or "")[-800:],
                "restored": True}

    check = _validate_tree(plugin_dir)
    if not check["ok"]:
        # Preserve the generated (rejected) tree for inspection instead of deleting it: when php is not
        # installed the validator uses a crude brace/paren check that can false-positive, so we need to
        # be able to see what Claude Code actually produced before trusting the rollback.
        rejected = plugin_dir.parent / (plugin_dir.name + "-rejected-"
                                        + datetime.now().strftime("%Y%m%d-%H%M%S"))
        try:
            shutil.move(str(plugin_dir), str(rejected))
        except Exception:
            shutil.rmtree(plugin_dir, ignore_errors=True)
            rejected = None
        shutil.move(str(backup), str(plugin_dir))
        return {**plan, "ok": False, "applied": False, "reason": "validation_failed",
                "problems": check["problems"], "restored": True,
                "rejected_dir": (str(rejected) if rejected else None), "php_lint": check.get("php_lint"),
                "hint": "The generated change did not pass validation and was rolled back. "
                        "Refine the spec and try again."}

    changed = [rel for rel, m in
               {str(f.relative_to(plugin_dir)): f.stat().st_mtime
                for f in _php_files(plugin_dir)}.items()
               if before.get(rel) != m or rel not in before]
    if not changed:
        # The agent ran and exited cleanly but edited NOTHING. Reporting "updated the source" here
        # and letting the build proceed re-zips the OLD code and presents it as the implemented spec,
        # the exact false-green M has been hunting. Roll back to the pristine backup and fail honestly,
        # surfacing the agent's own output so the cause (plan-only, spec unclear, auth) is visible.
        shutil.rmtree(plugin_dir, ignore_errors=True)
        shutil.move(str(backup), str(plugin_dir))
        return {**plan, "ok": False, "applied": False, "reason": "no_changes",
                "output_tail": (output or "")[-800:], "restored": True,
                "hint": "Claude Code ran but changed no plugin files, so nothing was built. Likely "
                        "causes: it responded with a plan instead of editing, the spec was too vague, "
                        "or headless auth/tools were unavailable. The run output above shows which."}
    return {**plan, "ok": True, "applied": True, "validation": check,
            "changed_files": changed, "backup": str(backup),
            "output_tail": (output or "")[-400:],
            "next": "Review if you like, then: wp_plugin_action build, then deploy. Nothing "
                    "has been shipped yet."}
