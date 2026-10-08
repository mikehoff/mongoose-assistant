from __future__ import annotations

from typing import Any

# Browser setup wizard backend.
#
# The cockpit wizard is the FRONT DOOR until setup is done. To avoid two sources of
# truth (the exact bug we spent versions removing), it is driven by the SAME
# INTEGRATIONS registry as the CLI wizard: this module just projects that registry
# into JSON the browser can render, and routes saves back through the same write_env +
# verifier path the CLI uses. The browser is free to present it differently (staged
# cards, progress, in-page verify), but WHAT exists, the integrations and their fields,
# comes from one place.

# Stages mirror the CLI wizard's logical grouping (core -> data -> reach/run -> remote),
# but presentation belongs to the browser.
# Stages mirror the CLI wizard's logical grouping, but presentation belongs to the
# browser. IMPORTANT (vision): Mongoose's CORE is the local brain, database, model and
# safe operation, NOT any integration. Home Assistant is a strong "Home" domain you can
# skip, never the gate to the whole assistant. So the wizard's configurable stages are
# all optional domains; "core readiness" is a separate, computed local-health signal.
WIZARD_STAGES: list[dict[str, Any]] = [
    {"key": "home", "title": "Your home",
     "blurb": "Connect Home Assistant so Mongoose can see and help with your home. Totally optional, skip it and everything else still works.",
     "slugs": ["home_assistant"]},
    {"key": "data", "title": "Your world",
     "blurb": "Let Mongoose in on your calendar, email and tasks, so it can actually be useful day to day. Add one, add all, or none.",
     "slugs": ["google_calendar", "microsoft", "imap"]},
    {"key": "reach", "title": "Stay in touch",
     "blurb": "Get nudges on your phone and let Mongoose quietly keep things up to date in the background.",
     "slugs": ["push", "scheduler"]},
    {"key": "remote", "title": "Anywhere & beyond",
     "blurb": "Reach Mongoose from your phone on the go. Setting up Dynamic DNS + HTTPS here is also what unlocks inviting guests by link, so it's worth doing. Optionally let it call on bigger models for the hard questions.",
     "slugs": ["remote", "ddns", "gemini", "anthropic", "openai", "alexa"]},
]


def _integration_by_slug() -> dict[str, Any]:
    from .setup_wizard import INTEGRATIONS
    return {i.slug: i for i in INTEGRATIONS}


# Integrations whose "Save & test" verifier contacts a remote provider. Local-first
# honesty: the UI labels these so the user knows a test will leave the machine. Home
# Assistant is LAN (not internet) but still off-box, so it's flagged too.
_EXTERNAL_VERIFIER_SLUGS = {
    "gemini", "microsoft", "anthropic", "openai", "push", "home_assistant",
}


def _verifier_is_external(slug: str) -> bool:
    return slug in _EXTERNAL_VERIFIER_SLUGS


def _is_configured(integ, env: dict[str, str]) -> bool:
    """An integration counts as configured only when there is real user-supplied evidence.

    Defaults copied from .env.example are scaffolding, not proof. This mirrors the CLI/status truth
    model: required fields must contain non-empty, non-default values; all-optional integrations need at
    least one non-empty, non-default value. Integrations with configured_when remain authoritative
    (OAuth tokens, DDNS provider selection, etc.).
    """
    if getattr(integ, "configured_when", None) is not None:
        try:
            return bool(integ.configured_when(env))
        except Exception:
            return False
    def _is_set(f) -> bool:
        v = env.get(f.key, "")
        return bool(v) and v != getattr(f, "default", "")
    required = [f for f in integ.fields if not f.optional]
    if required:
        return all(_is_set(f) for f in required)
    return any(_is_set(f) for f in integ.fields)


def _auth_state(integ, env: dict[str, str]):
    """Three-state OAuth status ("not_set"|"needs_signin"|"signed_in") if the integration declares
    one, else None. Lets the UI show 'sign in needed' instead of a false 'configured'."""
    fn = getattr(integ, "auth_state", None)
    if fn is None:
        return None
    try:
        return fn(env)
    except Exception:
        return None


def _core_status() -> dict[str, Any]:
    """The genuine core of Mongoose: the local brain (model/Ollama) and the database.
    This is computed health, not integration config, no smart home or cloud account is
    part of the core. core_done means the local assistant can actually run, which is the
    only thing that should gate the cockpit front door."""
    items = []
    brain_ok = True
    db_ok = True
    try:
        from .doctor import run_doctor
        # Use a generous timeout: a tight 1s probe reported "not ready" on a perfectly working Mac
        # just because Ollama (loaded, possibly busy with a large model) didn't answer /api/tags
        # within 1s. The smoke test passing 'llm chat' while the cockpit said "core not ready" was
        # exactly this, a probe-timeout false negative, not a real outage.
        checks = run_doctor(timeout_seconds=6.0).get("checks", {})
        ollama = checks.get("ollama", {})
        # Reachable + chat model present is enough for the FRONT-DOOR gate. The doctor's stricter ok
        # also requires the exact tool/code model names to be pulled; missing those is a setup nudge,
        # not "core down". Gating the whole cockpit on an exact tool-model name match was wrong.
        reachable = ollama.get("error") is None and "not reachable" not in str(ollama.get("detail", "")).lower()
        present = ollama.get("present", {}) if isinstance(ollama.get("present"), dict) else {}
        chat_ok = present.get("chat", True)  # if we can't tell, don't block on it
        brain_ok = bool(ollama.get("ok")) or bool(reachable and chat_ok)
        # Definitive override: if the brain has actually produced a reply recently, it IS working ,
        # no probe result should say "core not ready" while the user is successfully chatting. This
        # catches any remaining model-name/presence edge case the /api/tags check can't see.
        if not brain_ok and reachable:
            try:
                from .db import connect
                from datetime import datetime, timezone, timedelta
                with connect() as _con:
                    row = _con.execute(
                        "SELECT MAX(created_at) AS m FROM messages WHERE role='assistant'").fetchone()
                if row and row["m"]:
                    last = datetime.fromisoformat(str(row["m"]).replace("Z", "+00:00"))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - last) < timedelta(hours=24):
                        brain_ok = True
            except Exception:
                pass
        detail = ollama.get("detail", "")
        if not brain_ok and reachable:
            detail = "Ollama reachable but the chat model isn't pulled yet. Run: ollama pull <model>"
        items.append({"label": "Local brain (Ollama)", "ok": brain_ok, "detail": detail})
        db = checks.get("database", {})
        # database check may be absent; absence is not failure (db self-heals)
        db_ok = bool(db.get("ok", True))
        items.append({"label": "Database", "ok": db_ok, "detail": db.get("detail", "")})
    except Exception as exc:
        items.append({"label": "Core check", "ok": False, "detail": str(exc)})
        brain_ok = False
    # The database is always-present (it self-heals on first run); the brain is the real
    # gate. We treat core as ready when the local brain is reachable.
    return {"items": items, "ready": brain_ok and db_ok}


def wizard_schema() -> dict[str, Any]:
    """Project the registry + current values into JSON for the browser wizard. Secret
    values are never sent back as plaintext, only whether they are set."""
    from .setup_wizard import effective_env
    env = effective_env()
    by_slug = _integration_by_slug()
    stages = []
    for stage in WIZARD_STAGES:
        integrations = []
        for slug in stage["slugs"]:
            integ = by_slug.get(slug)
            if not integ:
                continue
            fields = []
            for f in integ.fields:
                val = env.get(f.key, "")
                fields.append({
                    "key": f.key, "prompt": f.prompt, "secret": f.secret,
                    "optional": f.optional, "default": f.default,
                    # never leak a secret's value; just whether it's set
                    "value": ("" if f.secret else val),
                    "is_set": bool(val),
                })
            integrations.append({
                "slug": integ.slug, "name": integ.name, "help": integ.help_text,
                "fields": fields, "has_verifier": integ.verifier is not None,
                "external_check": _verifier_is_external(integ.slug),
                "configured": _is_configured(integ, env),
                "auth_state": _auth_state(integ, env),
            })
        stages.append({"key": stage["key"], "title": stage["title"],
                       "blurb": stage["blurb"], "integrations": integrations})
    # core_done = the LOCAL core is operational (brain + db), NOT any integration. This
    # is what gates the cockpit front door, so an unconfigured smart home or cloud
    # account never traps the user in setup.
    core = _core_status()
    configured_count = sum(
        1 for st in stages for ig in st["integrations"] if ig["configured"]
    )
    return {"stages": stages, "core_done": core["ready"], "core_status": core,
            "configured_count": configured_count}


def save_integration(slug: str, values: dict[str, str]) -> dict[str, Any]:
    """Persist one integration's fields via the SAME write_env path the CLI uses, then
    run its verifier if present. Empty values are ignored (don't clobber). Secret
    fields left blank are left as-is (the browser sends blank to mean 'unchanged')."""
    from .setup_wizard import _read_env, write_env, INTEGRATIONS, effective_env
    integ = next((i for i in INTEGRATIONS if i.slug == slug), None)
    if integ is None:
        return {"ok": False, "error": f"unknown integration: {slug}"}
    env = _read_env()
    updates: dict[str, str] = {}
    for f in integ.fields:
        if f.key not in values:
            continue
        v = (values.get(f.key) or "").strip()
        if v:
            updates[f.key] = v
        # blank secret = leave unchanged; blank non-secret with no prior = skip
    if updates:
        marker = "MONGOOSE_SETUP_SAVED_" + slug.upper().replace("-", "_")
        updates[marker] = "1"
        try:
            write_env(updates)
        except ValueError as exc:
            # write_env rejects newline/CR injection; surface it cleanly, not as a 500
            return {"ok": False, "error": str(exc)}
        env.update(updates)
        # Also push the saved values into the live process environment, so the RUNNING server
        # uses them immediately, no restart needed, and so an empty/stale os.environ value can't
        # block load_dotenv(override=False) from ever seeing the new .env value. Without this, a
        # credential could read as "configured" in the wizard yet still be unused by HA/mail/etc.
        import os
        for k, v in updates.items():
            os.environ[k] = v
    result: dict[str, Any] = {"ok": True, "saved": list(updates.keys())}
    if integ.verifier:
        try:
            check_env = {**effective_env(), **updates}
            ok, msg = integ.verifier(check_env)
            result["verified"] = ok
            result["detail"] = msg
        except Exception as exc:
            result["verified"] = False
            result["detail"] = f"verify failed: {exc}"
    # Kick off an INITIAL sync so first search/brief has data immediately, instead of waiting for the
    # 30-min scheduled sync (which was why "the first search surfaces nothing"). Fire-and-forget in a
    # thread so the save response stays fast; only for connectors that have a sync, and only if it looks
    # configured. Never raises into the save.
    try:
        if updates and result.get("verified", True) is not False:
            _SYNCABLE = {"imap": "sync_email", "microsoft": "sync_tasks",
                         "gmail": "sync_email", "google_calendar": "sync_tasks"}
            job = _SYNCABLE.get(slug)
            if job:
                import threading
                def _kick(jobname=job):
                    try:
                        from .services.scheduler import run_job_now
                        run_job_now(jobname)
                    except Exception:
                        pass
                threading.Thread(target=_kick, daemon=True).start()
                result["initial_sync_started"] = job
    except Exception:
        pass
    # Return a flat integrations list so the frontend can update badges in-place without a round-trip.
    # NOTE: we build this WITHOUT calling wizard_schema(), that calls run_doctor() which is 6s.
    # Instead we build the flat list directly from the INTEGRATIONS registry + current env.
    try:
        from .setup_wizard import effective_env, INTEGRATIONS
        snap_env = effective_env()
        flat = []
        for integ in INTEGRATIONS:
            flat.append({
                "slug": integ.slug,
                "name": integ.name,
                "configured": _is_configured(integ, snap_env),
                "auth_state": _auth_state(integ, snap_env),
            })
        result["integrations"] = flat
    except Exception:
        pass  # badge refresh is best-effort; save itself already succeeded
    return result


def verify_integration(slug: str) -> dict[str, Any]:
    """Run an integration's verifier against current env without saving."""
    from .setup_wizard import effective_env, INTEGRATIONS
    integ = next((i for i in INTEGRATIONS if i.slug == slug), None)
    if integ is None:
        return {"ok": False, "error": f"unknown integration: {slug}"}
    if not integ.verifier:
        return {"ok": True, "verified": None, "detail": "no verifier for this integration"}
    env = effective_env()
    try:
        ok, msg = integ.verifier(env)
        return {"ok": True, "verified": ok, "detail": msg}
    except Exception as exc:
        return {"ok": False, "verified": False, "detail": f"verify failed: {exc}"}
