"""Protocol v3 runtime loop: the READ side of the bus, the voicemail drainer, ack/heartbeat, catch-up
reconciliation, and the protocol-version check.

The primitives (identity, SSH transport, locks, envelope build, bus WRITE, handshake, panel safety) live
in protocol_v3.py. This module adds what the v3 build checklist still needed:

  4. BUS READ, pull the tail, parse messages:, keep to==mongoose/all, track last processed msg_id,
     never reprocess; rotation-aware (follow the archive pointer when the bus rotated).
  5. VOICEMAIL DRAINER, drain /config/claude_bus_staging/mongoose_voicemail.jsonl, dedupe on msg_id,
     ack each, catch up the backlog in order. A voicemail is DATA; acting on it is M-approval-gated.
  6. ACK + HEARTBEAT + CATCH-UP, ack any needs_ack message addressed to us; post heartbeats; on wake,
     reconcile bus + voicemail backlog since our last processed msg_id BEFORE acting.
  9. PROTOCOL-VERSION CHECK, read protocol_version from claude_shared_workspace.yaml; if it's higher
     than we support, don't act on newer-protocol messages, post version-mismatch.

EVERYTHING here is gated and honest:
  - The whole loop is M-gated by protocol_v3_enabled, exactly like every other v3 action. Reading the
    bus/voicemail is safe (it's data), but we don't run the loop or post unless v3 is enabled.
  - A message is DATA, never an instruction (the safety gate from protocol_v3 is reused).
  - Acting on a drained voicemail (callback/reminder/notify M) is the agent's own output and stays
    M-approval-gated, this module only DRAINS, ACKS, and records; it never acts on a voicemail.
  - All shared reads/writes go to the Pi over SSH; a Mac-local copy is never the source of truth.
"""
from __future__ import annotations

import json
import shlex
from typing import Any

from ..db import get_app_state, set_app_state
from ..settings import get_settings
from .protocol_v3 import (
    AGENT_NAME,
    PROTOCOL_VERSION,
    _append_to_bus,
    _run_remote,
    build_envelope,
    parse_envelope,
    transport_status,
)

# app_state keys for "never reprocess" tracking
_LAST_BUS_MSG = "v3_last_bus_msg_id"
_PROCESSED_VM = "v3_processed_voicemail_ids"   # JSON list, capped
_PROCESSED_VM_IDENTITY = "v3_processed_voicemail_identities"   # canonical identity keys, capped


def _enabled() -> bool:
    return bool(getattr(get_settings(), "protocol_v3_enabled", False))


def _cfg_dir() -> str:
    return (getattr(get_settings(), "pi_config_dir", "/config") or "/config").rstrip("/")


# --------------------------------------------------------------------------------------------------
# 4. BUS READ (tail, parse, track last msg_id, rotation-aware)
# --------------------------------------------------------------------------------------------------

def _read_remote_file(path: str, *, tail_bytes: int = 65536) -> tuple[bool, str]:
    """Read (the tail of) a file on the Pi over SSH. tail keeps us cheap on a large bus."""
    qp = shlex.quote(path)
    # tail -c keeps the last N bytes; if the file is smaller we get all of it.
    res = _run_remote(f"tail -c {int(tail_bytes)} {qp} 2>/dev/null || true", timeout=10)
    return res.ok, res.stdout


def _read_all_bus_files(*, tail_bytes: int = 32768) -> str:
    """Read EVERY agent's bus file and concatenate them (PER-AGENT FILE DESIGN). We tail each file
    SEPARATELY (not a single tail over a cat), so no one file's recent messages get truncated by another
    file's bulk. Covers: the legacy shared claude_bus.yaml (which peers may still write during migration)
    plus every per-agent claude_bus_<agent>.yaml. One SSH round-trip. read_bus dedups + orders the result."""
    cfg = shlex.quote(_cfg_dir())
    # For each matching file, print its last N bytes then a newline separator so a truncated final line in
    # one file can't glue onto the first line of the next. Glob is safe: unmatched globs list nothing here
    # because we guard with [ -f ]. claude_bus.yaml has no underscore so it won't double-match the glob.
    cmd = (
        "for f in " + cfg + "/claude_bus.yaml " + cfg + "/claude_bus_*.yaml; do "
        "[ -f \"$f\" ] && tail -c " + str(int(tail_bytes)) + " \"$f\" && echo; "
        "done 2>/dev/null || true"
    )
    res = _run_remote(cmd, timeout=12)
    return res.stdout or ""


def _parse_bus_messages(text: str) -> list[dict[str, Any]]:
    """Parse the bus into a list of envelope dicts. The bus is append-only YAML where each entry is a
    '- {json}' line (how _append_to_bus writes). We parse line-by-line so a single malformed entry can't
    break the whole read, a robustness rule from the checklist. Falls back to a YAML parse for entries
    written by other agents in block style."""
    messages: list[dict[str, Any]] = []
    # First try the line-oriented '- {json}' form (what mongoose writes; cheap + robust).
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("- "):
            continue
        body = s[2:].strip()
        if not body.startswith("{"):
            continue
        try:
            messages.append(json.loads(body))
        except Exception:
            continue
    # If we found nothing but there's clearly content, try a full YAML parse (block-style messages from
    # chat/code). Best-effort; never raises.
    if not messages and ("messages:" in text or "protocol_version" in text):
        try:
            import yaml  # PyYAML is a dependency for HA YAML work
            doc = yaml.safe_load(text)
            if isinstance(doc, dict) and isinstance(doc.get("messages"), list):
                messages = [m for m in doc["messages"] if isinstance(m, dict)]
            elif isinstance(doc, list):
                messages = [m for m in doc if isinstance(m, dict)]
        except Exception:
            pass
    return messages


def _archive_pointer(text: str) -> str | None:
    """If the bus rotated, an agent leaves a pointer to claude_bus_archive_<ISO>.yaml. Detect it so a
    history read can follow it. We only need the newest archive name mentioned."""
    import re
    names = re.findall(r"claude_bus_archive_[\w:.+-]+\.yaml", text)
    return names[-1] if names else None


def read_bus(*, only_for_me: bool = True, since_last: bool = True) -> dict[str, Any]:
    """Pull the bus tail, parse it, and return the messages addressed to us (to==mongoose/all), newest
    last. With since_last=True, only messages after the last msg_id we processed (never reprocess).
    Read-only: takes no lock (reading doesn't need one), changes nothing on the Pi. Does NOT advance the
    cursor, call mark_processed() after you've handled them."""
    if not _enabled():
        return {"ok": True, "enabled": False, "messages": [], "detail": "v3 not enabled."}
    t = transport_status()
    if not t.get("reachable"):
        return {"ok": False, "reachable": False, "messages": [],
                "detail": "Pi not reachable; can't read the bus."}
    text = _read_all_bus_files()
    raw = _parse_bus_messages(text)
    parsed = [parse_envelope(m) for m in raw]
    # PER-AGENT FILE DESIGN: we read the legacy shared bus AND every per-agent file, so a message could
    # surface more than once (e.g. an agent mid-migration writing both). Dedup by msg_id, then order by
    # timestamp (msg_id is timestamp-prefixed, so it's a safe fallback) to get one stable append log
    # across all files , the cursor below relies on that stable order.
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for p in parsed:
        mid = p.get("msg_id")
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        uniq.append(p)
    uniq.sort(key=lambda m: str(m.get("timestamp") or m.get("msg_id") or ""))
    # "for me" means FROM ANOTHER agent, to me or all. We now read mongoose's OWN file too, so exclude
    # self-authored broadcasts (to==all) that would otherwise echo back as inbound.
    msgs = [p for p in uniq
            if (not only_for_me or (p.get("_for_me") and p.get("from") != AGENT_NAME))]

    last = get_app_state(_LAST_BUS_MSG, "") or ""
    if since_last and last:
        # keep only messages strictly after the last one we processed (by position in the appended log)
        ids = [m.get("msg_id") for m in msgs]
        if last in ids:
            idx = ids.index(last)
            msgs = msgs[idx + 1:]
    return {
        "ok": True, "enabled": True, "reachable": True,
        "messages": msgs,
        "count": len(msgs),
        "archive_pointer": _archive_pointer(text),
        "last_processed": last or None,
    }


def mark_processed(msg_id: str) -> None:
    """Advance the 'last processed bus msg' cursor so we never reprocess. Idempotent."""
    if msg_id:
        set_app_state(_LAST_BUS_MSG, msg_id)


# --------------------------------------------------------------------------------------------------
# 6. ACK + HEARTBEAT
# --------------------------------------------------------------------------------------------------

def post_ack(of_msg_id: str, *, to: str = "all") -> dict[str, Any]:
    """Append an ack envelope for a needs_ack message addressed to us. M-gated. If the original sender
    isn't a bus peer (e.g. a voicemail from 'ha/example_court'), the ack goes to 'all', the ack_of
    field carries the identity, and VALID_RECIPIENTS only knows the three agents."""
    if not _enabled():
        return {"ok": True, "posted": False, "detail": "v3 not enabled."}
    peer = to if to in ("chat", "code", "mongoose", "all") else "all"
    env = build_envelope(topic="ack", body=f"mongoose ack of {of_msg_id}", to=peer,
                         needs_ack=False, ack_of=of_msg_id)
    res = _append_to_bus(env)
    return {"ok": res.get("ok", False), "posted": res.get("ok", False), "ack_of": of_msg_id}


def post_lock_reclaim(target: str, previous_sid: str | None) -> dict[str, Any]:
    """When acquire_lock reclaims a STALE lock it returns must_post='lock-reclaim'; the rule is that the
    reclaimer MUST announce it on the bus. This posts that message. M-gated."""
    if not _enabled():
        return {"ok": True, "posted": False, "detail": "v3 not enabled."}
    env = build_envelope(
        topic="lock-reclaim",
        body=(f"mongoose reclaimed a stale lock on {target}"
              + (f" previously held by {previous_sid}" if previous_sid else "") + "."),
        to="all", needs_ack=False, refs=[target])
    res = _append_to_bus(env)
    return {"ok": res.get("ok", False), "posted": res.get("ok", False)}


def take_lock_and_announce(target: str, ttl_seconds: int = 60) -> dict[str, Any]:
    """Acquire a Pi lock and, if it was a STALE reclaim, automatically post the required lock-reclaim
    message, closing the 'caller must post' gap so a reclaim is never silent. Returns the lock result
    plus whether a reclaim was announced."""
    from .protocol_v3 import acquire_lock
    lock = acquire_lock(target, ttl_seconds=ttl_seconds)
    announced = False
    if lock.get("ok") and lock.get("reclaimed"):
        r = post_lock_reclaim(target, lock.get("previous_sid"))
        announced = r.get("posted", False)
    lock["reclaim_announced"] = announced
    return lock


def post_heartbeat() -> dict[str, Any]:
    """Write mongoose's liveness to its OWN file (OVERWRITE), NOT the shared bus. A heartbeat is liveness,
    not a message: appending one every few minutes FLOODS the shared coordination bus and drowns the real
    messages (peers reported exactly that). And the shared bus is a multi-writer race across separate
    systems , notably chat's HA MCP save_config_file is a full-file REWRITE that clobbers anything appended
    since its last read , so keeping mongoose's most frequent write OFF the shared file both declutters it
    and removes mongoose from the contention. A per-agent liveness file has no clobber (only mongoose
    writes it) and needs no lock. Peers read <pi_config_dir>/claude_heartbeat_mongoose.json for 'is
    mongoose alive' (check alive_at freshness). M-gated."""
    if not _enabled():
        return {"ok": True, "posted": False, "detail": "v3 not enabled."}
    if not transport_status().get("reachable"):
        return {"ok": True, "posted": False, "detail": "Pi unreachable; no heartbeat."}
    import json as _json
    import shlex as _shlex
    from datetime import datetime, timezone
    try:
        from .. import __version__ as _ver
    except Exception:
        _ver = ""
    cfg = (getattr(get_settings(), "pi_config_dir", "/config") or "/config").rstrip("/")
    path = f"{cfg}/claude_heartbeat_{AGENT_NAME}.json"
    rec = {"agent": AGENT_NAME, "protocol_version": PROTOCOL_VERSION, "version": _ver,
           "alive_at": datetime.now(timezone.utc).isoformat()}
    # OVERWRITE with a single "last alive" record (>, not >>), so it never grows and only mongoose writes
    # it , no lock, no clobber, no bus noise.
    res = _run_remote(f"printf '%s\\n' {_shlex.quote(_json.dumps(rec, ensure_ascii=False))} > {_shlex.quote(path)}")
    return {"ok": res.ok, "posted": res.ok, "path": path,
            "detail": res.detail or (f"wrote liveness to {path}" if res.ok else (res.stderr or "")[:160])}


# --------------------------------------------------------------------------------------------------
# 5. VOICEMAIL DRAINER (Example Court RTM voicemails)
# --------------------------------------------------------------------------------------------------

def _processed_vm_ids() -> set[str]:
    raw = get_app_state(_PROCESSED_VM, "") or ""
    try:
        return set(json.loads(raw)) if raw else set()
    except Exception:
        return set()


def _remember_vm(vm_id: str) -> None:
    # Use an ordered list so the [-500:] cap preserves the MOST RECENT entries.
    # Sets are unordered: converting set → list[-500:] can arbitrarily drop recent ids.
    existing = _processed_vm_ids()
    if vm_id in existing:
        return  # already there
    ids_list = json.loads(get_app_state(_PROCESSED_VM, "") or "[]")
    if not isinstance(ids_list, list):
        ids_list = list(existing)
    ids_list.append(vm_id)
    set_app_state(_PROCESSED_VM, json.dumps(ids_list[-500:]))


def _processed_vm_identities() -> set[str]:
    raw = get_app_state(_PROCESSED_VM_IDENTITY, "") or ""
    try:
        return set(json.loads(raw)) if raw else set()
    except Exception:
        return set()


def _remember_vm_identity(identity_key: str) -> None:
    existing = _processed_vm_identities()
    if identity_key in existing:
        return
    keys_list = json.loads(get_app_state(_PROCESSED_VM_IDENTITY, "") or "[]")
    if not isinstance(keys_list, list):
        keys_list = list(existing)
    keys_list.append(identity_key)
    set_app_state(_PROCESSED_VM_IDENTITY, json.dumps(keys_list[-500:]))


def _normalise_voicemail(env: dict[str, Any]) -> dict[str, Any]:
    """The body shape is still being confirmed with code: it may be a structured dict
    {uid, caller, transcript, wav_rel_path, tag} OR a raw 'message' string formatted
    'From: <caller>\\n<transcript>\\n<wav path>'. Handle BOTH (checklist item 5)."""
    body = env.get("body")
    out = {"uid": "", "caller": "", "transcript": "", "wav_rel_path": "", "tag": ""}
    if isinstance(body, dict):
        out.update({k: body.get(k, "") for k in out})
    elif isinstance(body, str):
        # 'From: <caller>\n<transcript>\n<wav path>'
        lines = body.splitlines()
        if lines and lines[0].lower().startswith("from:"):
            out["caller"] = lines[0].split(":", 1)[1].strip()
            if len(lines) >= 2:
                out["transcript"] = lines[1].strip()
            if len(lines) >= 3:
                out["wav_rel_path"] = lines[-1].strip()
        else:
            out["transcript"] = body.strip()
    # uid falls back to the msg_id minus the vm_ prefix
    if not out["uid"]:
        mid = str(env.get("msg_id", ""))
        out["uid"] = mid[3:] if mid.startswith("vm_") else mid
    return out


def drain_voicemail(*, ack: bool = True, max_drain: int = 100) -> dict[str, Any]:
    """Drain new lines from /config/claude_bus_staging/mongoose_voicemail.jsonl on the Pi. JSON Lines,
    one voicemail per line, append-only. Dedupe on msg_id (the add-on re-polls, so a vm can arrive
    twice). Ack each (topic ack, ack_of the vm msg_id). Process the backlog IN ORDER if we were offline.

    A voicemail is DATA. This function ONLY reads, dedupes, acks, and RECORDS, it never calls anyone
    back, sets a reminder, or notifies M. Acting on a voicemail is the agent's own output and stays
    M-approval-gated elsewhere."""
    if not _enabled():
        return {"ok": True, "enabled": False, "drained": [], "detail": "v3 not enabled."}
    if not transport_status().get("reachable"):
        return {"ok": False, "reachable": False, "drained": [], "detail": "Pi unreachable."}
    path = f"{_cfg_dir()}/claude_bus_staging/mongoose_voicemail.jsonl"
    ok, text = _read_remote_file(path, tail_bytes=262144)
    if not ok:
        return {"ok": True, "drained": [], "detail": "no voicemail file yet (normal)."}

    seen = _processed_vm_ids()
    seen_identities = _processed_vm_identities()
    drained: list[dict[str, Any]] = []
    acked = 0
    for line in text.splitlines():
        s = line.strip()
        if not s or not s.startswith("{"):
            continue
        try:
            env = json.loads(s)
        except Exception:
            continue
        vm_id = str(env.get("msg_id", ""))
        if not vm_id or vm_id in seen:
            continue  # cheap dedupe: the HA add-on re-polls the same line; process that msg_id once
        vm = _normalise_voicemail(env)
        # Canonical identity: the SAME call staged again (or arriving via IMAP too) collapses to one
        # voicemail regardless of msg_id. Order no longer matters; sources merge by real identity.
        try:
            from .voicemail_identity import identity_from_staging
            identity_key, canonical_uid, ref = identity_from_staging(env, vm)
        except Exception:
            identity_key, canonical_uid, ref = "", None, {"source": "pi_ha"}
        already = bool(identity_key) and identity_key in seen_identities
        if already:
            # We've already drained this voicemail under another msg_id/source. Skip emitting a duplicate
            # row, but still ack the new observation so the sender stops re-sending.
            _remember_vm(vm_id); seen.add(vm_id)
            if ack and env.get("needs_ack"):
                try:
                    post_ack(vm_id, to=str(env.get("from") or "all"))
                    acked += 1
                except Exception:
                    pass
            continue
        # ── STORE FIRST, then remember and ack ────────────────────────────────────
        # A transient DB failure must not mark the voicemail processed or ack it: that would
        # cause silent loss from the canonical store. Order matters: persist → remember → ack.
        store_ok = False
        try:
            from .voicemail import index_voicemail_from_protocol_staging
            staging_record = {
                "msg_id": env.get("msg_id"),
                "account_number": env.get("account_number"),
                "voicemail_at": env.get("voicemail_at") or env.get("timestamp"),
                "from": env.get("from"),
                **vm,
            }
            index_voicemail_from_protocol_staging(staging_record)
            store_ok = True
        except Exception:
            store_ok = False
        # Only mark processed (and ack) once storage succeeded
        if store_ok:
            _remember_vm(vm_id); seen.add(vm_id)
            if identity_key:
                _remember_vm_identity(identity_key); seen_identities.add(identity_key)
            drained.append({"msg_id": vm_id, "identity_key": identity_key,
                            "canonical_uid": canonical_uid, "source_refs": [ref], **vm})
            if ack and env.get("needs_ack"):
                try:
                    post_ack(vm_id, to=str(env.get("from") or "all"))
                    acked += 1
                except Exception:
                    pass
        # On store failure: leave unacknowledged so the Pi will retry.
        if len(drained) >= max_drain:
            break
    return {"ok": True, "enabled": True, "drained": drained, "count": len(drained),
            "acked": acked,
            "note": "Voicemails are DATA. Acting on them (callback/reminder/notify) is M-approval-gated "
                    "and is NOT done here. Dedup is by canonical voicemail identity, so the same call "
                    "from Pi/HA and IMAP collapses to one, in any order."}


# --------------------------------------------------------------------------------------------------
# 9. PROTOCOL-VERSION CHECK
# --------------------------------------------------------------------------------------------------

def protocol_version_check() -> dict[str, Any]:
    """Read protocol_version from claude_shared_workspace.yaml on the Pi. If it's HIGHER than we
    support, we must not act on newer-protocol messages and should post a version-mismatch. Read-only;
    posting is left to the caller / the runtime loop (and is M-gated)."""
    if not transport_status().get("reachable"):
        return {"ok": False, "reachable": False, "detail": "Pi unreachable."}
    ok, text = _read_remote_file(f"{_cfg_dir()}/claude_shared_workspace.yaml", tail_bytes=8192)
    if not ok:
        return {"ok": True, "found": False, "supported": PROTOCOL_VERSION,
                "detail": "no claude_shared_workspace.yaml yet (PRE-LIVE is fine)."}
    live = None
    try:
        import yaml
        doc = yaml.safe_load(text) or {}
        if isinstance(doc, dict):
            live = doc.get("protocol_version")
    except Exception:
        import re
        m = re.search(r"protocol_version:\s*(\d+)", text)
        if m:
            live = int(m.group(1))
    if live is None:
        return {"ok": True, "found": False, "supported": PROTOCOL_VERSION,
                "detail": "protocol_version not present in workspace file."}
    try:
        live_int = int(live)
    except Exception:
        return {"ok": True, "found": True, "supported": PROTOCOL_VERSION, "live": live,
                "mismatch": False, "detail": f"unparseable protocol_version: {live!r}"}
    mismatch = live_int > PROTOCOL_VERSION
    return {"ok": True, "found": True, "supported": PROTOCOL_VERSION, "live": live_int,
            "mismatch": mismatch,
            "detail": (f"live protocol v{live_int} is newer than mongoose supports (v{PROTOCOL_VERSION}); "
                       f"won't act on newer-protocol messages." if mismatch
                       else f"live protocol v{live_int}; mongoose supports v{PROTOCOL_VERSION}.")}


def post_version_mismatch(live: int) -> dict[str, Any]:
    if not _enabled():
        return {"ok": True, "posted": False, "detail": "v3 not enabled."}
    env = build_envelope(
        topic="version-mismatch",
        body=(f"mongoose supports protocol v{PROTOCOL_VERSION} but the workspace declares v{live}; "
              f"mongoose will not act on newer-protocol messages until upgraded."),
        to="all", needs_ack=False)
    res = _append_to_bus(env)
    return {"ok": res.get("ok", False), "posted": res.get("ok", False)}


# --------------------------------------------------------------------------------------------------
# 6. CATCH-UP / the wake reconcile loop
# --------------------------------------------------------------------------------------------------

def reconcile_on_wake(*, post_heartbeat_too: bool = True) -> dict[str, Any]:
    """The wake routine: BEFORE acting, reconcile the bus + voicemail backlog since our last processed
    msg_id, ack anything addressed to us that needs it, run the protocol-version check, and (optionally)
    post a heartbeat. Returns a summary. M-gated; safe to call on a schedule.

    Order matters (checklist item 6): catch up FIRST, then we're current. This function does the
    catching-up and acking; it deliberately does NOT take any household action, that stays M-gated."""
    if not _enabled():
        return {"ok": True, "enabled": False, "detail": "v3 not enabled; nothing to reconcile."}
    summary: dict[str, Any] = {"ok": True, "enabled": True}

    # protocol-version check first, if the world moved past us, say so and stop acting on new msgs.
    pv = protocol_version_check()
    summary["protocol_version"] = pv
    if pv.get("mismatch"):
        post_version_mismatch(int(pv.get("live")))
        summary["version_mismatch_posted"] = True

    # bus catch-up: read everything for us since last, ack the needs_ack ones, advance the cursor.
    bus = read_bus(only_for_me=True, since_last=True)
    new_msgs = bus.get("messages", [])
    acked = 0
    for m in new_msgs:
        if m.get("needs_ack") and m.get("from") != AGENT_NAME:
            post_ack(str(m.get("msg_id")), to=str(m.get("from") or "all"))
            acked += 1
    if new_msgs:
        mark_processed(str(new_msgs[-1].get("msg_id")))
    summary["bus"] = {"new": len(new_msgs), "acked": acked,
                      "archive_pointer": bus.get("archive_pointer")}

    # voicemail backlog
    vm = drain_voicemail(ack=True)
    summary["voicemail"] = {"drained": vm.get("count", 0), "acked": vm.get("acked", 0)}

    if post_heartbeat_too:
        hb = post_heartbeat()
        summary["heartbeat_posted"] = hb.get("posted", False)

    summary["detail"] = (f"Caught up: {len(new_msgs)} bus message(s), "
                         f"{vm.get('count', 0)} voicemail(s). Household actions remain M-gated.")
    return summary
