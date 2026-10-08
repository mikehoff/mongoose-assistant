from __future__ import annotations

import json
import time
import tempfile
import socket
import os
import shutil
import stat
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__


def current_version() -> str:
    return __version__


def version_of(path: Path) -> str | None:
    """Read the version from a new-version folder's pyproject.toml."""
    pyproject = path / "pyproject.toml"
    if not pyproject.exists():
        return None
    for line in pyproject.read_text().splitlines():
        line = line.strip()
        if line.startswith("version"):
            parts = line.split('"')
            if len(parts) >= 2:
                return parts[1]
    return None




def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stage_root() -> Path:
    from .settings import get_settings
    root = get_settings().data_dir / "update_stages"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _stage_id(version: str | None = None) -> str:
    suffix = (version or "unknown").replace(".", "-")
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{suffix}-" + uuid.uuid4().hex[:6]


def _safe_unzip(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            pure_parts = Path(name).parts
            if not name or name.startswith(("/", "\\")) or ".." in pure_parts:
                raise ValueError(f"unsafe zip member path: {name!r}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ValueError(f"unsafe zip member symlink: {name!r}")
        z.extractall(dest)


def _find_candidate_root(base: Path) -> Path | None:
    if (base / "pyproject.toml").exists() and (base / "src" / "mongoose").exists():
        return base
    for p in sorted(base.rglob("pyproject.toml")):
        root = p.parent
        if (root / "src" / "mongoose").exists():
            return root
    return None


def _read_json_file(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_stage(stage_id: str) -> dict[str, Any] | None:
    path = _stage_root() / stage_id / "stage.json"
    if not path.exists():
        return None
    data = _read_json_file(path, None)
    return data if isinstance(data, dict) else None


def _save_stage(stage: dict[str, Any]) -> None:
    _write_json_file(_stage_root() / stage["stage_id"] / "stage.json", stage)


def stage_update_candidate(source: str | Path) -> dict[str, Any]:
    """Stage a candidate update into data/update_stages without applying it.

    This is the self-updater's safe floor: copy/extract, verify manifest/version,
    write stage metadata, but do not touch the live tree.
    """
    src = Path(source).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"update source not found: {src}")
    tmp_id = _stage_id(None)
    stage_dir = _stage_root() / tmp_id
    incoming = stage_dir / "candidate"
    incoming.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        _safe_unzip(src, incoming)
    elif src.is_dir():
        shutil.copytree(src, incoming / src.name, symlinks=False, ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc", ".git", "data", "secrets", "certs", ".env", ".coverage"))
    else:
        raise ValueError(f"unsupported update source: {src}")
    root = _find_candidate_root(incoming)
    if not root:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise ValueError("candidate does not contain pyproject.toml plus src/mongoose")
    target_version = version_of(root)
    # Rename the stage folder to include the target version once known.
    final_id = _stage_id(target_version)
    final_dir = _stage_root() / final_id
    stage_dir.rename(final_dir)
    root = final_dir / root.relative_to(stage_dir)
    manifest = {"ok": True, "problems": []}
    try:
        from .services.release_manifest import verify_against_manifest
        manifest = verify_against_manifest(root)
    except Exception as exc:
        manifest = {"ok": False, "problems": [f"manifest check failed: {exc}"]}
    stage = {
        "stage_id": final_id,
        "status": "staged",
        "approved": False,
        "source": str(src),
        "candidate_root": str(root),
        "current_version": current_version(),
        "target_version": target_version,
        "manifest": manifest,
        "checks": [],
        "created_at": _now(),
        "updated_at": _now(),
        "apply_command": f"mongoose update --apply-stage {final_id} --yes",
        "rollback_command": "mongoose update --rollback",
    }
    _save_stage(stage)
    return stage


def _resolve_update_url() -> tuple[str, str]:
    """Resolve where to fetch the next update from. Returns (url, kind).

    Precedence:
      1. MONGOOSE_UPDATE_URL : a direct zip URL (kind='direct')
      2. MONGOOSE_UPDATE_GITHUB_REPO ('owner/name'): latest release zip via GitHub API (kind='github')
    Empty/unset => ('', '').
    """
    direct = (os.getenv("MONGOOSE_UPDATE_URL", "") or "").strip()
    if direct:
        return direct, "direct"
    repo = (os.getenv("MONGOOSE_UPDATE_GITHUB_REPO", "") or "").strip()
    if repo:
        return f"https://api.github.com/repos/{repo}/releases/latest", "github"
    return "", ""


def _github_zip_from_release(api_url: str, *, timeout: float = 10.0) -> tuple[str, str]:
    """Read a GitHub 'latest release' API doc and return (zip_url, tag). Prefers an attached
    .zip asset; falls back to the source zipball. Returns ('','') if nothing usable."""
    import urllib.request
    req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "mongoose-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - configured update source
        doc = json.loads(resp.read().decode("utf-8"))
    tag = str(doc.get("tag_name") or doc.get("name") or "")
    for asset in doc.get("assets", []) or []:
        name = (asset.get("name") or "").lower()
        if name.endswith(".zip") and asset.get("browser_download_url"):
            return asset["browser_download_url"], tag
    if doc.get("zipball_url"):
        return doc["zipball_url"], tag
    return "", tag


def fetch_update_candidate(source_url: str | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
    """Fetch an update zip FROM WITHIN Mongoose and stage it (no apply).

    This is the piece that lets the next update originate from Mongoose itself rather than a human
    upload: it downloads a release zip from the configured source, then runs it through the same
    stage_update_candidate() safety floor (manifest/version verification, no live-tree changes).
    The human-gated approve/apply/rollback steps are unchanged, so 'self-update' never means
    'unsupervised self-modification'.

    Returns the stage dict on success, or {ok:False, error:...}. Never raises.
    """
    try:
        url = (source_url or "").strip()
        kind = "direct"
        if not url:
            url, kind = _resolve_update_url()
        if not url:
            return {"ok": False, "error": "no update source configured "
                    "(set MONGOOSE_UPDATE_URL or MONGOOSE_UPDATE_GITHUB_REPO)"}
        tag = ""
        if kind == "github":
            zip_url, tag = _github_zip_from_release(url, timeout=timeout)
            if not zip_url:
                return {"ok": False, "error": f"no downloadable zip in GitHub release ({url})"}
            url = zip_url
        # Download to a temp file
        import urllib.request
        dest = Path(tempfile.mkdtemp()) / "mongoose-update.zip"
        req = urllib.request.Request(url, headers={"User-Agent": "mongoose-updater"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - configured update source
            data = resp.read()
        if not data or data[:2] != b"PK":
            return {"ok": False, "error": "downloaded file is not a zip archive"}
        dest.write_bytes(data)
        # Hand off to the existing safe staging path
        stage = stage_update_candidate(dest)
        stage["fetched_from"] = url
        if tag:
            stage["release_tag"] = tag
        # Refuse a no-op / downgrade up front (clear signal, not a silent stage)
        cur = current_version()
        tgt = stage.get("target_version") or ""
        stage["is_newer"] = bool(tgt and tgt != cur)
        _save_stage(stage)
        return {"ok": True, **stage}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def list_update_stages(limit: int = 20) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in sorted(_stage_root().glob("*/stage.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        d = _read_json_file(p, {})
        if isinstance(d, dict):
            rows.append(d)
    return rows[:limit]


def check_update_stage(stage_id: str, *, runtime_smoke: bool = False) -> dict[str, Any]:
    stage = _load_stage(stage_id)
    if not stage:
        raise ValueError(f"unknown update stage: {stage_id}")
    root = Path(stage["candidate_root"])
    checks: list[dict[str, Any]] = []
    def run(name: str, argv: list[str]) -> dict[str, Any]:
        try:
            r = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=90)
            return {"name": name, "ok": r.returncode == 0, "returncode": r.returncode,
                    "stdout": (r.stdout or "")[-2000:], "stderr": (r.stderr or "")[-2000:]}
        except Exception as exc:
            return {"name": name, "ok": False, "error": str(exc)}
    checks.append({"name": "manifest", "ok": bool(stage.get("manifest", {}).get("ok", False)), "detail": stage.get("manifest")})
    checks.append(run("compileall", [sys.executable, "-m", "compileall", "-q", "src", "tests"]))
    checks.append(run("pyflakes", [sys.executable, "-m", "pyflakes", "src", "tests"]))
    # Optional runtime smoke: actually boot the candidate on a temp port and verify it serves correct
    # runtime identity BEFORE this version is applied (the "installed vs actually running" claim, made
    # safe at update time). Off by default since it spawns a process.
    if runtime_smoke:
        try:
            smoke = runtime_smoke_stage(stage_id)
            checks.append({"name": "runtime_smoke", "ok": bool(smoke.get("ok")), "detail": smoke})
        except Exception as exc:
            checks.append({"name": "runtime_smoke", "ok": False, "error": str(exc)})
    ok = all(c.get("ok") for c in checks)
    stage["checks"] = checks
    stage["status"] = "checked" if ok else "failed_checks"
    stage["updated_at"] = _now()
    _save_stage(stage)
    return {"stage_id": stage_id, "ok": ok, "checks": checks, "status": stage["status"]}


def approve_update_stage(stage_id: str) -> dict[str, Any]:
    stage = _load_stage(stage_id)
    if not stage:
        raise ValueError(f"unknown update stage: {stage_id}")
    stage["approved"] = True
    stage["status"] = "approved"
    stage["updated_at"] = _now()
    _save_stage(stage)
    return stage


def apply_update_stage(stage_id: str, *, yes: bool = False) -> int:
    stage = _load_stage(stage_id)
    if not stage:
        print(f"Unknown update stage: {stage_id}")
        return 1
    if not stage.get("approved"):
        print("Refusing to apply: stage is not approved. Run `mongoose update --approve-stage " + stage_id + "` first.")
        return 1
    if not stage.get("checks") or not all(c.get("ok") for c in stage.get("checks", [])):
        print("Refusing to apply: stage checks have not all passed. Run `mongoose update --check-stage " + stage_id + "` first.")
        return 1
    if not yes:
        print("This stage is approved and checked, but not applied.")
        print("Run exactly this to apply with the normal backup/rollback updater:")
        print("  mongoose update --apply-stage " + stage_id + " --yes")
        return 0
    # BRAIN PRESERVATION happens centrally inside run_update_guided (below), so it covers BOTH the
    # staged path and a direct `mongoose update <src>`. We just record the stage outcome here.
    root = stage["candidate_root"]
    # Pause the external watchdog across the apply: applying restarts the server, and we don't want the
    # watchdog to see /health/live briefly fail during that restart and spawn a SECOND server. resume
    # happens when the new server's `serve` starts (it lifts the pause), with a finally as a backstop.
    _wd_paused = False
    try:
        from .services.server_watchdog import pause as _wd_pause
        _wd_pause("update apply")
        _wd_paused = True
    except Exception:
        pass
    try:
        rc = run_update_guided(root, install_missing="never", assume_yes=True)
        if rc == 0:
            stage["status"] = "applied"
            stage["applied_at"] = _now()
            _save_stage(stage)
        return rc
    finally:
        # Backstop: if the new server didn't come up to lift the pause, resume after a delay so the
        # watchdog isn't left muzzled forever. The new serve also lifts it; whichever happens first.
        if _wd_paused:
            try:
                from .services.server_watchdog import resume as _wd_resume
                import threading as _th
                _th.Timer(120.0, _wd_resume).start()
            except Exception:
                pass


def run_update_flag(flag: str) -> int:
    """Pass a flag like --rollback or --list-backups to scripts/update.sh."""
    script = Path("scripts/update.sh")
    if not script.exists():
        print("scripts/update.sh not found. Run this from your Mongoose install directory.")
        return 1
    try:
        return subprocess.run(["bash", str(script), flag], check=False).returncode
    except Exception as exc:
        print(f"Update command failed: {exc}")
        return 1


def owner_network_postflight() -> dict[str, Any]:
    """Sections 1-6 of the guided update prove the CODE and the DISK are coherent, and that
    SOMETHING answers on loopback. None of that proves the owner's phone can actually reach
    this Mac: a process can serve a correct /health on 127.0.0.1 while still being genuinely
    loopback-only on the interface a router's port-forward reaches, which is exactly the
    failure class an update is meant to fix, not just carry forward unexamined.

    Standalone (not inlined into run_update_guided) so it is independently testable and
    reusable from anywhere else that wants the same real check, rather than each caller
    re-deriving it. Reuses the same functions already live in the running app (owner
    baseline, the LAN-facing listener check, the DDNS reachability diagnosis); this adds no
    new diagnostic logic of its own, only wires the existing, already-tested ones into the
    update path, where none of them were being called before this.

    Returns {ok, lines, ddns_configured}: `lines` are ready-to-print status strings; `ok` is
    False whenever the LAN listener isn't genuinely accepting or a configured DDNS route
    isn't confirmed reachable."""
    lines: list[str] = []
    ok = True
    try:
        from .owner_defaults import ensure_owner_baseline
        baseline = ensure_owner_baseline(source="update_postflight")
        if baseline.get("updates"):
            lines.append(f"Owner baseline: applied ({', '.join(baseline['updates'].keys())}); "
                         "takes effect on the NEXT restart, not the one that already ran.")
        else:
            lines.append("Owner baseline: already in place.")
    except Exception as exc:
        lines.append(f"(owner baseline check skipped: {exc})")
    try:
        from .ddns import public_listener_acceptance, reachability_diagnosis
        from .setup_wizard import effective_env
        listener = public_listener_acceptance()
        if listener.get("loopback_accepting") and not listener.get("lan_accepting"):
            lines.append("LAN listener: FAILING. Loopback answers, but the LAN-facing address "
                         "does not; a router's port-forward reaches the LAN address, not "
                         "loopback, so this is the exact cause of ERR_CONNECTION_REFUSED even "
                         "with a correctly configured router. Restart Mongoose again; this "
                         "cannot be fixed by disk changes alone.")
            ok = False
        elif not listener.get("listener_accepting"):
            lines.append("LAN listener: not accepting at all yet (may still be starting up).")
            ok = False
        else:
            lines.append("LAN listener: accepting on both loopback and the LAN-facing address.")
        env = effective_env()
        ddns_configured = bool(env.get("DDNS_PROVIDER"))
        if ddns_configured:
            diag = reachability_diagnosis(env)
            hostname_suffix = f" ({diag.get('hostname')})" if diag.get("hostname") else ""
            lines.append(f"DDNS route: {diag.get('reason', 'unknown')}{hostname_suffix}")
            if not diag.get("ok"):
                ok = False
        else:
            lines.append("DDNS route: not configured (LAN-only install; nothing to check here).")
    except Exception as exc:
        lines.append(f"(network/DDNS postflight check failed to run: {exc})")
        ok = False
        ddns_configured = False
    return {"ok": ok, "lines": lines, "ddns_configured": ddns_configured}


def run_update(source: str) -> int:
    """Invoke scripts/update.sh with the given source folder/zip.

    Wrapped so users run `mongoose update <path>` instead of fiddly shell,
    avoiding quoting pitfalls. Returns the script's exit code.
    """
    script = Path("scripts/update.sh")
    if not script.exists():
        print("scripts/update.sh not found. Run this from your Mongoose install directory.")
        return 1
    print(f"Current version: {current_version()}")
    new_path = Path(source)
    if new_path.is_dir():
        nv = version_of(new_path)
        if nv:
            print(f"Target version: {nv}")
    try:
        result = subprocess.run(["bash", str(script), source], check=False)
        return result.returncode
    except Exception as exc:
        print(f"Update failed to start: {exc}")
        return 1


def run_update_guided(source: str, install_missing: str = "ask", *, assume_yes: bool = False) -> int:
    """A comprehensive, guided update: preflight summary, the update itself, then
    postflight (merge any new .env keys, apply DB migrations, readiness check).
    This makes `mongoose update` safe and self-explaining rather than a bare shell
    call, so you always know what changed and that the result is healthy."""
    print("=" * 60)
    print("  Mongoose update")
    print("=" * 60)
    cur = current_version()
    new_path = Path(source)
    target = version_of(new_path) if new_path.is_dir() else None
    print(f"\n  From: {cur}")
    print(f"  To:   {target or '(version unknown until applied)'}")
    print("\n  This will: back up your install, swap in the new code, then merge any")
    print("  new settings keys, apply database migrations, and run a readiness check.")
    print("  Your brain (conversation, memory, identity) lives in your home folder and")
    print("  is preserved across the update, a verified snapshot is taken first as a backup.\n")
    if not assume_yes:
        if input("  Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  Cancelled.")
            return 1
    else:
        print("  Proceeding because the staged update was explicitly approved and --yes was supplied.")

    # Preflight: if the incoming source is an unpacked tree, verify it against its own
    # release.json (version + command surface) so we don't apply an incoherent/stale
    # tree. The post-mortem showed a 0.5.4 tree applied while claiming success.
    try:
        from .services.release_manifest import verify_against_manifest
        if new_path.is_dir():
            pre = verify_against_manifest(new_path)
            if not pre["ok"]:
                print("\n  Refusing to update: the incoming release looks incoherent:")
                for p in pre.get("problems", []):
                    print(f"    - {p}")
                print("  Use a verified current zip, or scripts/fresh_restore.sh.")
                return 1
            if pre.get("manifest"):
                print(f"  Verified incoming release {pre['manifest'].get('version')} against its manifest.")
    except Exception as exc:
        print(f"  (manifest preflight skipped: {exc})")

    # BRAIN PRESERVATION (covers every update route, staged or direct). The brain , the permanent
    # conversation, long-term memory, learned routing preferences, identity continuity , is well
    # contained in data/ and must NOT be disrupted by a code update. Before swapping code and running
    # the migration: verify the brain is healthy and take a CONSISTENT snapshot (SQLite online backup,
    # safe even while the server runs). The update preserves data/ in place (the code swap operates
    # around it); the snapshot is the brain-specific rollback point if the migration ever damages it.
    brain_snapshot_dir = None
    try:
        from .brain_preservation import preserve_around_update
        bp = preserve_around_update()
        pre = bp.get("pre_verify", {})
        if not pre.get("ok", True):
            print("  Note: the brain showed integrity problems BEFORE the update: "
                  + "; ".join(pre.get("problems", [])))
        if bp.get("snapshot", {}).get("ok"):
            brain_snapshot_dir = bp["snapshot"]["snapshot_dir"]
            from .brain_preservation import brain_paths
            brain_loc = brain_paths().get("data_dir")
            print("\n  Your brain is safe.")
            print(f"    - It lives in your home folder ({brain_loc}), OUTSIDE this install,")
            print("      so swapping in new code does not touch your conversation, memory or identity.")
            print(f"    - A verified snapshot was also taken as a rollback point: {brain_snapshot_dir}")
        else:
            print("  Warning: could not snapshot the brain; the full-tree backup still protects you.")
    except Exception as exc:  # noqa: BLE001
        print(f"  Warning: brain snapshot step errored ({exc}); continuing, full-tree backup still applies.")

    rc = run_update(source)
    if rc != 0:
        print(f"\n  Update script exited with code {rc}. Your previous install is backed up;")
        print("  use `mongoose update --rollback` if needed.")
        return rc

    print("\n  Post-update steps:")
    # 1. Merge any new env keys the new version introduced.
    try:
        from .setup_wizard import merge_new_env_keys
        added = merge_new_env_keys()
        if added:
            print(f"    - Added {len(added)} new setting(s) to .env: {', '.join(added)}")
            print("      (review them; they have safe defaults)")
        else:
            print("    - No new settings to add.")
    except Exception as exc:
        print(f"    - Could not merge new settings: {exc}")
    # 2. Apply DB migrations (idempotent).
    try:
        from .db import init_db
        init_db()
        print("    - Database migrations applied.")
    except Exception as exc:
        print(f"    - Migration step reported: {exc}")
    # 2b. BRAIN INTEGRITY CHECK: the migration just touched the live brain DB. Confirm the brain is
    # still intact (integrity + the continuity-bearing tables readable). If it isn't, the brain can be
    # rolled back INDEPENDENTLY of the code from the snapshot taken before the update.
    try:
        from .brain_preservation import verify_brain
        post = verify_brain()
        if post.get("ok"):
            print("    - Brain verified intact: your conversation, memory and identity carried across unharmed.")
        else:
            print("    - WARNING: brain failed verification after migration: "
                  + "; ".join(post.get("problems", [])))
            if brain_snapshot_dir:
                print(f"      Roll the brain back (independent of code): mongoose brain restore {brain_snapshot_dir}")
    except Exception as exc:  # noqa: BLE001
        print(f"    - (brain verification step errored: {exc})")
    # 2c. INTEGRATION PRESERVATION: an update preserves .env and secrets/ in place, but verify it , 
    # a missing HA_URL/HA_TOKEN or Google token after an update would make integrations look "wiped".
    # If anything that was configured is now absent, say so loudly with the fix, rather than letting
    # the user discover it as a silent disconnect.
    try:
        from pathlib import Path as _P
        from .settings import get_settings as _gs
        s = _gs()
        gone = []
        if not (getattr(s, "ha_url", "") and getattr(s, "ha_token", "")):
            gone.append("Home Assistant (HA_URL/HA_TOKEN)")
        tok = getattr(s, "google_token_path", "")
        if tok and not _P(tok).exists():
            gone.append("Google sign-in token (Calendar/Tasks/Gmail/Drive)")
        if gone:
            print("    - NOTE: these integrations are not currently configured/signed-in: "
                  + "; ".join(gone) + ".")
            print("      If they worked before this update, your .env/secrets may not have carried "
                  "across; restore from the backup taken above, or re-enter them in Setup.")
        else:
            print("    - Integrations preserved: Home Assistant and Google config carried across.")
    except Exception as exc:  # noqa: BLE001
        print(f"    - (integration preservation check errored: {exc})")
    # 3. Capability dependencies: a new version may add a feature needing new libs.
    try:
        from .services.capabilities import capabilities_report, install_all_missing
        rep = capabilities_report()
        if rep["missing"]:
            print("\n  New or unmet capability libraries:")
            for m in rep["missing"]:
                print(f"    - {m['capability']}: {m['missing']}")
            do_install = False
            if install_missing == "auto":
                do_install = True
            elif install_missing == "never":
                print("    (skipping install; --no-install set)")
            else:  # ask
                try:
                    resp = input("  Install missing libraries into the active environment now? [Y/n] ").strip().lower()
                    do_install = resp in ("", "y", "yes")
                except EOFError:
                    do_install = False
            if do_install:
                for r in install_all_missing():
                    if r.get("ok") and r.get("installed"):
                        print(f"    installed for {r['capability']}: {', '.join(r['installed'])}")
                    elif r.get("skipped_commands"):
                        print(f"    {r['capability']}: needs system command(s) {', '.join(r['skipped_commands'])}")
                    elif not r.get("ok"):
                        print(f"    FAILED for {r['capability']}: {r.get('error', 'unknown')}")
            else:
                print("    Not installed. Run `mongoose install-missing` later, or "
                      "`mongoose doctor --capabilities` to see what's needed.")
        else:
            print("\n  All capability libraries present.")
    except Exception as exc:
        print(f"  - Capability check reported: {exc}")
    # 4. Readiness check.
    print("\n  Readiness check:")
    try:
        from .setup_wizard import _print_doctor_summary
        _print_doctor_summary()
    except Exception as exc:
        print(f"    (couldn't run readiness check: {exc})")
    # 5. Install-coherence postflight. A passing readiness check is NOT enough: the
    # benchmark post-mortem showed an update can report success while the source tree
    # is stale (wrong version, missing commands). Verify the install is coherent and
    # warn loudly if not, pointing at a fresh restore.
    try:
        from .services.install_check import check_install_coherence
        coh = check_install_coherence()
        if coh["ok"]:
            print(f"\n  Install coherence: OK (version {coh['running_version']}, "
                  f"{coh['command_count']} commands).")
        else:
            print("\n  WARNING: install looks INCOHERENT after update:")
            for p in coh["problems"]:
                print(f"    - {p}")
            print(f"    {coh['hint']}")
    except Exception as exc:
        print(f"  (couldn't run install-coherence check: {exc})")
    # 6. LIVE-SERVER postflight. Disk coherence is provably insufficient: the field case had the
    # tree at the new version while the PORT kept serving the old one for weeks, because nothing
    # ever restarted the process or asked the port who it was. update.sh now performs the
    # handover; this independently verifies it and refuses to call the update successful otherwise.
    live = verify_live_version()
    if live.get("ok"):
        print(f"\n  Live server verified: version {live['live_version']} answering on port {live['port']}.")
    else:
        print("\n  UPDATE NOT COMPLETE: the running server does not match this install.")
        print(f"    expected: {live.get('expected')}")
        print(f"    live:     {live.get('live_version') or '(no answer on port ' + str(live.get('port')) + ')'}")
        print("    The old process may still hold the port. Diagnose with:")
        print(f"      lsof -n -P -iTCP:{live.get('port')} -sTCP:LISTEN")
        print("    then re-run the update, which now tears down and restarts the server itself.")
        return 1
    # 7. Owner network/DDNS route postflight. See owner_network_postflight()'s own docstring
    # for why sections 1-6 above are not enough to prove the owner's phone can reach this Mac.
    print("\n  Owner network/DDNS route postflight:")
    postflight = owner_network_postflight()
    for line in postflight["lines"]:
        print(f"    {line}")
    if not postflight["ok"]:
        print("\n  Code updated, but the phone/DDNS route is not confirmed working.")
        print("  This is not a code-update failure; rolling back would not fix a network")
        print("  issue. Open Cockpit -> Setup -> Remote access, or ask a paired phone to call")
        print("  the repair action, to finish this.")
    print("\n  Update complete. If anything looks off, `mongoose update --rollback`.")
    return 0


def verify_live_version(expected: str | None = None, port: int | None = None,
                        timeout: float = 20.0) -> dict[str, Any]:
    """Ask the live port which version is ACTUALLY serving and compare to this tree's version.

    Returns {ok, expected, live_version, port}. ok is True only when the port answers /health as
    mongoose with exactly the expected version within the timeout. Probes https first then http,
    ignoring self-signed certificate validity (identity here is the /health body, not the cert)."""
    import ssl as _ssl
    import time as _time
    import urllib.request as _ur
    import json as _json
    if expected is None:
        expected = current_version()
    if port is None:
        try:
            from .settings import get_settings
            port = int(getattr(get_settings(), "port", 8765) or 8765)
        except Exception:
            port = 8765
    live_version = None
    deadline = _time.time() + max(1.0, timeout)
    while _time.time() < deadline:
        for scheme in ("https", "http"):
            try:
                ctx = _ssl._create_unverified_context() if scheme == "https" else None
                with _ur.urlopen(f"{scheme}://127.0.0.1:{port}/health", timeout=2.0, context=ctx) as resp:
                    body = resp.read(4000).decode("utf-8", "replace")
                try:
                    data = _json.loads(body)
                except Exception:
                    data = {}
                if data.get("name") == "mongoose":
                    live_version = str(data.get("version") or "")
                    if live_version == expected:
                        return {"ok": True, "expected": expected, "live_version": live_version, "port": port}
            except Exception:
                continue
        _time.sleep(1.0)
    return {"ok": False, "expected": expected, "live_version": live_version, "port": port}


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_get_json(url: str, *, timeout: float = 2.0) -> dict[str, Any]:
    import json as _json
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec - local smoke only
        raw = resp.read(200_000).decode("utf-8", "replace")
    try:
        return _json.loads(raw)
    except Exception:
        return {"raw": raw}


def runtime_smoke_stage(stage_id: str, *, port: int | None = None, timeout_seconds: float = 20.0) -> dict[str, Any]:
    """Start a staged candidate on a temporary localhost port and probe it.

    This is deliberately local and temporary. It does not touch the live server, does
    not use the live DB, and disables background scheduler/watchdog loops so the
    smoke is about runtime identity and basic serving, not live automation.
    """
    stage = _load_stage(stage_id)
    if not stage:
        raise ValueError(f"unknown update stage: {stage_id}")
    root = Path(stage["candidate_root"])
    if not root.exists():
        raise ValueError(f"candidate root missing: {root}")
    port = int(port or _free_local_port())
    with tempfile.TemporaryDirectory(prefix="mongoose-stage-smoke-") as td:
        tdp = Path(td)
        env = os.environ.copy()
        env.update({
            "PYTHONPATH": str(root / "src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
            "MONGOOSE_DB_PATH": str(tdp / "mongoose.sqlite3"),
            "MONGOOSE_DATA_DIR": str(tdp / "data"),
            "MONGOOSE_HOST": "127.0.0.1",
            "MONGOOSE_PORT": str(port),
            "MONGOOSE_INPROCESS_SCHEDULER": "false",
            "MONGOOSE_RUNTIME_WATCHDOG": "false",
            "MONGOOSE_NO_WATCHDOG": "true",   # a throwaway candidate must NOT spawn a supervising watchdog
            "MONGOOSE_ALLOW_REMOTE": "false",
        })
        proc = subprocess.Popen(
            # --no-tls: the candidate serves plain http on its temporary port, so the http probe below
            # works and we skip generating a self-signed cert for a server that lives for seconds.
            [sys.executable, "-m", "mongoose.cli", "serve", "--host", "127.0.0.1",
             "--port", str(port), "--no-tls"],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        health: dict[str, Any] | None = None
        started_at = time.monotonic()
        error = ""
        try:
            while time.monotonic() - started_at < timeout_seconds:
                if proc.poll() is not None:
                    out, err = proc.communicate(timeout=2)
                    error = (err or out or "process exited")[-2000:]
                    break
                try:
                    health = _http_get_json(f"http://127.0.0.1:{port}/health", timeout=1.0)
                    if health.get("ok") is True or health.get("status") in ("ok", "healthy") or health:
                        break
                except Exception:
                    time.sleep(0.25)
            if not health:
                ok = False
                detail = error or f"candidate did not answer /health on temporary port {port}"
            else:
                expected = stage.get("target_version")
                got = str(health.get("version") or health.get("mongoose_version") or "")
                ok = bool(not expected or not got or got == expected)
                detail = "candidate served /health" if ok else f"version mismatch: expected {expected}, got {got or 'unknown'}"
            # A second low-cost route catches import/runtime issues that /health alone can miss.
            system_ok = False
            try:
                sysmap = _http_get_json(f"http://127.0.0.1:{port}/cockpit/system-map", timeout=2.0)
                system_ok = isinstance(sysmap, dict) and not sysmap.get("error")
            except Exception:
                system_ok = False
            if ok and not system_ok:
                # Not a hard fail, because a minimal candidate might not have the full cockpit,
                # but record it as a warning for the user.
                detail += "; /cockpit/system-map did not complete"
            return {"name": "runtime_smoke", "ok": ok, "port": port, "health": health or {}, "system_map_ok": system_ok, "detail": detail}
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def rollback_drill_stage(stage_id: str) -> dict[str, Any]:
    """Dry-run the rollback story for a staged candidate.

    This does not apply anything. It verifies that the updater script exists and tells
    the user exactly what backup/rollback path would be used if this stage were applied.
    """
    stage = _load_stage(stage_id)
    if not stage:
        raise ValueError(f"unknown update stage: {stage_id}")
    script = Path("scripts/update.sh")
    backup_glob = sorted(Path("..").glob("mongoose-backup-*"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    checks = [
        {"name": "stage_exists", "ok": Path(stage.get("candidate_root", "")).exists()},
        {"name": "update_script_exists", "ok": script.exists()},
        {"name": "stage_checked", "ok": bool(stage.get("checks")) and all(c.get("ok") for c in stage.get("checks", []))},
        {"name": "stage_approved", "ok": bool(stage.get("approved"))},
    ]
    ok = all(c["ok"] for c in checks[:2])
    drill = {
        "stage_id": stage_id,
        "ok": ok,
        "checks": checks,
        "would_apply": f"mongoose update --apply-stage {stage_id} --yes",
        "would_rollback": "mongoose update --rollback",
        "existing_backups": [str(p) for p in backup_glob[:5]],
        "notes": [
            "This is a dry run: it did not apply the candidate and did not create or restore a backup.",
            "A real apply uses scripts/update.sh, which backs up the current install before replacing code.",
            "Rollback remains manual and explicit: mongoose update --rollback.",
        ],
        "created_at": _now(),
    }
    stage.setdefault("drills", []).append(drill)
    stage["updated_at"] = _now()
    _save_stage(stage)
    return drill
