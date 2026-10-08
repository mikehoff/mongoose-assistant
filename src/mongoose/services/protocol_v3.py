"""Protocol v3, three-agent Home Assistant coordination (mongoose + chat + code).

This module is the CAPABILITY to participate in the v3 protocol described in the owner's onboarding brief. It is
deliberately M-gated and inert by default:

  - It NEVER flips the live protocol to v3, never creates the live claude_bus.yaml, never adds mongoose
    to the ownership map. That cutover is the owner's decision (settings.protocol_v3_enabled + an explicit
    approved handshake), per the brief's "CURRENT STATUS".
  - Until M configures the Pi SSH transport (settings.pi_ssh_host etc.), every transport call returns a
    clean "not configured" result rather than touching anything.

What it DOES provide, so mongoose can honestly tell M "I support v3":
  1. The v3 message envelope (build / parse / validate), including version checking.
  2. A Pi-side transport over the system `ssh`/`scp` binaries (no new dependency; present on every Mac),
     so the bus and locks live on the PI where the other two agents can see them, a Mac-only lock is
     "worse than no lock" per the brief.
  3. The lock convention: atomic create-if-not-exists, TTL, stale-reclaim, release-only-your-own.
  4. The version-handshake message and a domain-ownership PROPOSAL for M to approve.

SAFETY (from the brief, enforced here):
  - The bus is DATA, not instructions: parse() never executes anything; a message claiming "M approved"
    is just a field, never trusted as approval (see relayed_approval_is_trustworthy()).
  - PANEL SAFETY entities are encoded here so any v3 action builder can refuse to touch them.
  - "Verify live state" is the caller's duty; this module reads, it does not assume.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..settings import get_settings

PROTOCOL_VERSION = 3
AGENT_NAME = "mongoose"
VALID_RECIPIENTS = {"chat", "code", "mongoose", "all"}

# Wall-panel entities that must NEVER be bulk-toggled or powered off (hardware protection). Any v3 action
# mongoose proposes must be checked against this set. Mirrors the rule shared by all three agents.
PANEL_SAFETY_ENTITIES = frozenset({
    "light.control_hub",
    "light.control_hub_2_kitchen",
    "light.control_hub_ensuite",
    "switch.control_hub_switch_1",
    "switch.control_hub_switch_2",
    "switch.control_hub_switch_3",
    "switch.control_hub_2_switch_1",
    "switch.control_hub_2_switch_2",
    "switch.control_hub_2_switch_3",
    "switch.control_hub_ensuite_switch_1",
    "switch.control_hub_ensuite_switch_2",
    "switch.control_hub_ensuite_switch_3",
})
# Areas where area_id sweeps are forbidden (must use verified panel-free groups instead).
PANEL_SAFETY_AREAS = frozenset({"kitchen", "bathroom", "ensuite"})

# Files that are TOOL-API-ONLY for all three agents , never hand-edit / never lock-and-write these.
TOOL_API_ONLY_FILES = frozenset({"automations.yaml", "scripts.yaml", "scenes.yaml"})


def _now_iso_bst() -> str:
    """ISO 8601 timestamp with timezone. The brief notes BST = UTC+1; we emit an explicit offset so the
    reader never has to guess, and so UTC and local are unambiguous."""
    return datetime.now(timezone.utc).astimezone().isoformat()


_SEQ = 0


def _next_seq() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def session_id() -> str:
    """A stable-per-run lock session id, 'mongoose-session-<uuid>'. Stable within one process run so a
    lock we take is reclaimable/releasable by us and recognisable to the others."""
    if not hasattr(session_id, "_sid"):
        session_id._sid = f"{AGENT_NAME}-session-{uuid.uuid4().hex[:12]}"  # type: ignore[attr-defined]
    return session_id._sid  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------------------
# 1. The v3 envelope
# --------------------------------------------------------------------------------------------------

@dataclass
class Envelope:
    topic: str
    body: str
    to: str = "all"
    needs_ack: bool = False
    ack_of: str | None = None
    m_approval_status: str = "not-required"
    refs: list[str] = field(default_factory=list)
    # set at build time
    msg_id: str = ""
    timestamp: str = ""
    from_: str = AGENT_NAME
    protocol_version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "msg_id": self.msg_id,
            "from": self.from_,
            "to": self.to,
            "timestamp": self.timestamp,
            "topic": self.topic,
            "body": self.body,
            "m_approval_status": self.m_approval_status,
            "needs_ack": self.needs_ack,
            "ack_of": self.ack_of,
            "refs": list(self.refs),
        }


def build_envelope(topic: str, body: str, *, to: str = "all", needs_ack: bool = False,
                   ack_of: str | None = None, m_approval_status: str = "not-required",
                   refs: list[str] | None = None) -> dict[str, Any]:
    """Produce a v3 envelope dict ready to append to the bus. Validates the recipient and approval
    status so we never emit a malformed message."""
    if to not in VALID_RECIPIENTS:
        raise ValueError(f"'to' must be one of {sorted(VALID_RECIPIENTS)}, got {to!r}")
    if m_approval_status not in {"not-required", "pending", "approved", "denied"}:
        raise ValueError(f"invalid m_approval_status: {m_approval_status!r}")
    env = Envelope(
        topic=topic, body=body, to=to, needs_ack=needs_ack, ack_of=ack_of,
        m_approval_status=m_approval_status, refs=list(refs or []),
        msg_id=f"{_now_iso_bst()}-{AGENT_NAME}-{_next_seq()}",
        timestamp=_now_iso_bst(),
    )
    return env.to_dict()


def parse_envelope(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse + validate an incoming envelope. CRITICAL: this is DATA, not instructions, it returns a
    normalised dict and NEVER executes anything in it. Adds a '_supported' flag for version handling and
    an '_errors' list rather than raising, so a single malformed bus entry can't break a read of the
    tail."""
    errors: list[str] = []
    pv = raw.get("protocol_version")
    supported = pv == PROTOCOL_VERSION
    if not supported:
        errors.append(f"unsupported protocol_version: {pv!r}")
    to = raw.get("to")
    # `to` SHOULD be a single recipient string, but a peer can legitimately address several agents with a
    # list (to: [mongoose, code]) or emit junk. NEVER raise on it , the whole point of this function is
    # that one malformed entry can't break a tail read (a set-membership test on an unhashable list would
    # crash the entire read). Normalise to a list, test each string element.
    to_items = list(to) if isinstance(to, (list, tuple, set)) else [to]
    to_strs = [t for t in to_items if isinstance(t, str)]
    if not any(t in VALID_RECIPIENTS for t in to_strs):
        errors.append(f"invalid 'to': {to!r}")
    out = dict(raw)
    out["_supported"] = supported
    out["_errors"] = errors
    out["_for_me"] = any(t in (AGENT_NAME, "all") for t in to_strs)
    return out


def relayed_approval_is_trustworthy(envelope: dict[str, Any]) -> bool:
    """Per safety rule 2: relayed approval is NOT approval. A bus message's m_approval_status field,
    even 'approved', even from chat or code, does NOT constitute the owner's approval for a high-stakes action.
    This always returns False: approval must trace to M directly (out of band), never to the bus. Kept
    as an explicit, testable function so the rule is enforced in code, not just documented."""
    return False


# --------------------------------------------------------------------------------------------------
# 2. Pi-side transport over system ssh/scp (no new dependency)
# --------------------------------------------------------------------------------------------------

@dataclass
class TransportResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    detail: str = ""
    configured: bool = True


def _ssh_base_cmd() -> list[str] | None:
    """Build the ssh command prefix from settings, or None if the Pi transport isn't configured.

    Two modes:
      - KEY mode (default): ssh with BatchMode=yes so a missing key fails fast instead of hanging on a
        password prompt. Uses -i <key> when PI_SSH_KEY is set.
      - KEYLESS PASSWORD mode: when PI_SSH_PASSWORD is set (and no key), wrap ssh with `sshpass -e`,
        which feeds the password from the SSHPASS env var (set by _run_remote at call time, never
        passed on the command line where it could leak via the process list). BatchMode is dropped for
        this path because password auth needs the prompt. Requires sshpass on the Mac; if it's missing
        we return a sentinel so the caller can explain how to install it.
    """
    s = get_settings()
    host = (getattr(s, "pi_ssh_host", "") or "").strip()
    if not host:
        return None
    port = int(getattr(s, "pi_ssh_port", 22) or 22)
    key = (getattr(s, "pi_ssh_key", "") or "").strip()
    password = (getattr(s, "pi_ssh_password", "") or "").strip()

    # Keyless password mode: only when a password is set AND no key is configured (a key is always
    # preferred when present , it's keyless-by-choice, not a fallback that overrides a working key).
    if password and not key:
        import shutil
        if not shutil.which("sshpass"):
            return ["__NEEDS_SSHPASS__"]  # sentinel; _run_remote turns this into a friendly message
        cmd = ["sshpass", "-e", "ssh",
               "-o", "ConnectTimeout=6",
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "PreferredAuthentications=password",
               "-o", "PubkeyAuthentication=no"]
        if port and port != 22:
            cmd += ["-p", str(port)]
        cmd.append(host)
        return cmd

    # Key mode (default): BatchMode so a missing key fails fast rather than hanging on a prompt.
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
           "-o", "StrictHostKeyChecking=accept-new"]
    if port and port != 22:
        cmd += ["-p", str(port)]
    if key:
        cmd += ["-i", key]
    cmd.append(host)
    return cmd


def _scp_base_cmd() -> list[str] | None:
    """Build the scp command prefix mirroring _ssh_base_cmd's auth (key or keyless-password mode),
    so peer file transfer reuses the same configured credentials. Returns None if unconfigured,
    or the __NEEDS_SSHPASS__ sentinel if password mode lacks sshpass."""
    s = get_settings()
    host = (getattr(s, "pi_ssh_host", "") or "").strip()
    if not host:
        return None
    port = int(getattr(s, "pi_ssh_port", 22) or 22)
    key = (getattr(s, "pi_ssh_key", "") or "").strip()
    password = (getattr(s, "pi_ssh_password", "") or "").strip()
    if password and not key:
        import shutil
        if not shutil.which("sshpass"):
            return ["__NEEDS_SSHPASS__"]
        cmd = ["sshpass", "-e", "scp",
               "-o", "ConnectTimeout=6",
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "PreferredAuthentications=password",
               "-o", "PubkeyAuthentication=no"]
        if port and port != 22:
            cmd += ["-P", str(port)]  # scp uses -P (capital), unlike ssh's -p
        return cmd
    cmd = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
           "-o", "StrictHostKeyChecking=accept-new"]
    if port and port != 22:
        cmd += ["-P", str(port)]
    if key:
        cmd += ["-i", key]
    return cmd


def _remote_file_sha256(remote_path: str) -> str | None:
    """The sha256 of a file on the peer, or None if it doesn't exist / can't be read.
    Used to skip a redundant transfer (file already matches) and to verify a landed file."""
    import shlex as _shlex
    q = _shlex.quote(remote_path)
    # shasum is present on macOS and most Linux; sha256sum on Linux. Try sha256sum then shasum.
    res = _run_remote(f"sha256sum {q} 2>/dev/null | cut -d' ' -f1 || shasum -a 256 {q} 2>/dev/null | cut -d' ' -f1")
    if not res.ok:
        return None
    digest = (res.stdout or "").strip()
    return digest if len(digest) == 64 else None


def push_file_to_peer(local_path: str, remote_path: str, *, verify: bool = True,
                      skip_if_same: bool = True, timeout: int = 60) -> dict[str, Any]:
    """Efficiently and verifiably copy a local file to a peer (the Pi) over scp.

    Efficiency + integrity properties the ad-hoc 'inline content into a remote shell command' path
    lacked:
      - skip_if_same: if the peer already has a byte-identical file (by sha256), do nothing and report
        'unchanged': no redundant transfer of code that hasn't changed.
      - verify: after the copy, re-hash the landed file and compare to the local hash, so a truncated
        or corrupted transfer is caught instead of silently shipping a broken file.
    Reuses the existing SSH credentials. Never raises; returns a structured result.
    """
    import hashlib
    import os as _os
    import shlex as _shlex
    from pathlib import Path as _P

    lp = _P(local_path)
    # An absurdly long path makes exists()/is_file() raise OSError (ENAMETOOLONG) before the
    # not-found branch can return; treat any such failure as 'not a usable local file'.
    try:
        is_file = lp.exists() and lp.is_file()
    except OSError:
        is_file = False
    if not is_file:
        return {"ok": False, "error": "local file not found or unreadable"}
    base = _scp_base_cmd()
    if base is None:
        return {"ok": False, "configured": False, "error": "Pi SSH transport not configured (set PI_SSH_HOST)."}
    if base == ["__NEEDS_SSHPASS__"]:
        return {"ok": False, "configured": True,
                "error": "Keyless password mode needs 'sshpass' on this Mac (brew install sshpass)."}

    local_hash = hashlib.sha256(lp.read_bytes()).hexdigest()

    if skip_if_same:
        remote_hash = _remote_file_sha256(remote_path)
        if remote_hash == local_hash:
            return {"ok": True, "unchanged": True, "transferred": False,
                    "sha256": local_hash, "detail": "peer already has an identical file"}

    s = get_settings()
    host = (getattr(s, "pi_ssh_host", "") or "").strip()
    # scp target: ensure the remote directory exists first (cheap, idempotent).
    remote_dir = _os.path.dirname(remote_path) or "."
    mk = _run_remote(f"mkdir -p {_shlex.quote(remote_dir)}", timeout=timeout)
    if not mk.ok:
        return {"ok": False, "error": f"couldn't ensure remote dir: {mk.detail or mk.stderr[:160]}"}

    target = f"{host}:{remote_path}"
    env = None
    if base and base[0] == "sshpass":
        env = dict(_os.environ)
        env["SSHPASS"] = (getattr(s, "pi_ssh_password", "") or "")
    try:
        proc = subprocess.run(base + [str(lp), target], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except FileNotFoundError:
        return {"ok": False, "error": "scp binary not found on this machine."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"scp timed out after {timeout}s."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"scp failed: {type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "error": f"scp exit {proc.returncode}: {(proc.stderr or '')[:200]}"}

    result = {"ok": True, "transferred": True, "unchanged": False, "sha256": local_hash,
              "remote_path": remote_path}
    if verify:
        landed = _remote_file_sha256(remote_path)
        result["verified"] = (landed == local_hash)
        if landed != local_hash:
            result["ok"] = False
            result["error"] = (f"verification failed: landed file hash {landed} != local {local_hash} "
                               "(transfer may be truncated/corrupt)")
    return result


def push_files_to_peer(files: list[tuple[str, str]], *, verify: bool = True,
                       skip_if_same: bool = True) -> dict[str, Any]:
    """Push several (local_path, remote_path) pairs to a peer, skipping any that are already
    identical. Reports per-file outcomes plus a summary so a code-sync only moves what changed.
    """
    results = []
    transferred = unchanged = failed = 0
    for local_path, remote_path in files:
        r = push_file_to_peer(local_path, remote_path, verify=verify, skip_if_same=skip_if_same)
        results.append({"local": local_path, "remote": remote_path, **r})
        if not r.get("ok"):
            failed += 1
        elif r.get("unchanged"):
            unchanged += 1
        else:
            transferred += 1
    return {"ok": failed == 0, "transferred": transferred, "unchanged": unchanged,
            "failed": failed, "total": len(files), "results": results}


def _run_remote(remote_cmd: str, *, timeout: int = 12) -> TransportResult:
    """Run a single command on the Pi over ssh. Returns a clean TransportResult; never raises for an
    expected failure (not configured, ssh missing, non-zero exit, timeout). In keyless password mode
    the password is passed to sshpass via the SSHPASS env var (never on argv, so it can't leak through
    the process list)."""
    base = _ssh_base_cmd()
    if base is None:
        return TransportResult(ok=False, configured=False,
                               detail="Pi SSH transport not configured (set PI_SSH_HOST).")
    if base == ["__NEEDS_SSHPASS__"]:
        return TransportResult(ok=False, configured=True,
                               detail="Keyless password mode needs 'sshpass' on this Mac. Install it "
                                      "with: brew install sshpass (or switch to key mode).")
    import os as _os
    env = None
    if base and base[0] == "sshpass":
        env = dict(_os.environ)
        env["SSHPASS"] = (getattr(get_settings(), "pi_ssh_password", "") or "")
    try:
        proc = subprocess.run(base + [remote_cmd], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except FileNotFoundError:
        return TransportResult(ok=False, detail="ssh binary not found on this machine.")
    except subprocess.TimeoutExpired:
        return TransportResult(ok=False, detail=f"ssh timed out after {timeout}s.")
    except Exception as exc:  # noqa: BLE001
        return TransportResult(ok=False, detail=f"ssh failed: {type(exc).__name__}: {exc}")
    return TransportResult(ok=(proc.returncode == 0), stdout=proc.stdout, stderr=proc.stderr,
                           detail="" if proc.returncode == 0 else f"exit {proc.returncode}")


def transport_status() -> dict[str, Any]:
    """Report whether the Pi transport is configured and reachable, without changing anything. Safe to
    call any time; used by the cockpit / a 'can I speak v3?' self-check."""
    base = _ssh_base_cmd()
    if base is None:
        return {"configured": False, "reachable": False, "host": "",
                "detail": "Pi SSH not configured (PI_SSH_HOST empty)."}
    # A harmless echo proves auth + connectivity without touching the bus or any lock.
    res = _run_remote("echo mongoose_v3_probe", timeout=8)
    reachable = res.ok and "mongoose_v3_probe" in res.stdout
    # The bug this replaces: res.detail is ALWAYS a truthy "exit N" on any non-zero exit, so
    # `res.detail or res.stderr` could never fall through to stderr, and the actual SSH reason
    # (e.g. "Permission denied (publickey).", "Connection refused", "Connection timed out") was
    # discarded in favor of a meaningless exit code -- exactly what showed up as "exit 255" with
    # no further explanation. Prefer the real stderr line whenever ssh produced one.
    stderr_line = (res.stderr or "").strip().splitlines()[-1].strip() if (res.stderr or "").strip() else ""
    if reachable:
        detail = "ok"
    elif stderr_line:
        detail = stderr_line[:200]
    else:
        detail = res.detail or "unreachable"
    return {"configured": True, "reachable": reachable,
            "host": (getattr(get_settings(), "pi_ssh_host", "") or ""),
            "detail": detail}


# --------------------------------------------------------------------------------------------------
# 3. Locks (on the Pi, atomic, TTL, stale-reclaim, release-only-your-own)
# --------------------------------------------------------------------------------------------------

def _lock_path(target: str) -> str:
    s = get_settings()
    cfg = (getattr(s, "pi_config_dir", "/config") or "/config").rstrip("/")
    # Locks live under /config/claude_locks/: the agreed directory for all three agents.
    # This matches what Chat and Code expect so cross-agent lock visibility is correct.
    safe = target.replace("/", "_")
    return f"{cfg}/claude_locks/{safe}"


def acquire_lock(target: str, ttl_seconds: int = 60) -> dict[str, Any]:
    """Atomically take <target>.lock on the Pi (create-if-not-exists via `set -C`/noclobber). If a LIVE
    lock is held by someone else, do not steal it. If the existing lock is STALE (acquired_at + ttl <
    now), reclaim it and signal that the caller must post a lock-reclaim message. Never edits a
    tool-API-only file's lock, those are never hand-written by anyone."""
    if target in TOOL_API_ONLY_FILES:
        return {"ok": False, "error": "tool_api_only",
                "detail": f"{target} is edited via the HA tool-API only; it is never lock-and-written."}
    path = _lock_path(target)
    # Ensure the claude_locks directory exists (first writer creates it; idempotent on Pi).
    lock_dir = path.rsplit("/", 1)[0]
    _run_remote(f"mkdir -p {shlex.quote(lock_dir)}")
    sid = session_id()
    payload = json.dumps({"sid": sid, "acquired_at": _now_iso_bst(), "ttl_seconds": ttl_seconds})
    q = shlex.quote(payload)
    qp = shlex.quote(path)
    # noclobber create: succeeds only if the lock does not already exist (atomic on a POSIX fs).
    create = f"set -o noclobber; printf '%s' {q} > {qp}"
    res = _run_remote(create)
    if res.ok:
        return {"ok": True, "acquired": True, "sid": sid, "path": path, "reclaimed": False}
    if not res.configured:
        return {"ok": False, "error": "not_configured", "detail": res.detail}
    # Lock exists , inspect it to decide live vs stale.
    cur = _run_remote(f"cat {qp}")
    if not cur.ok:
        return {"ok": False, "error": "exists_unreadable", "detail": cur.detail}
    try:
        held = json.loads(cur.stdout.strip() or "{}")
    except Exception:
        held = {}
    if held.get("sid") == sid:
        return {"ok": True, "acquired": True, "sid": sid, "path": path, "reclaimed": False,
                "detail": "already held by us"}
    if _lock_is_stale(held):
        # Reclaim: overwrite the stale lock. Caller MUST post a lock-reclaim message (rule).
        forced = f"printf '%s' {q} > {qp}"
        fres = _run_remote(forced)
        if fres.ok:
            return {"ok": True, "acquired": True, "sid": sid, "path": path, "reclaimed": True,
                    "previous_sid": held.get("sid"),
                    "must_post": "lock-reclaim"}
        return {"ok": False, "error": "reclaim_failed", "detail": fres.detail}
    return {"ok": False, "acquired": False, "error": "held",
            "holder_sid": held.get("sid"), "detail": "lock held by a live session; back off and retry."}


def release_lock(target: str) -> dict[str, Any]:
    """Release <target>.lock, but ONLY if we hold it (release-only-your-own). Reads the lock, checks the
    sid, and removes it only on a match."""
    path = _lock_path(target)
    qp = shlex.quote(path)
    cur = _run_remote(f"cat {qp} 2>/dev/null || true")
    if not cur.configured:
        return {"ok": False, "error": "not_configured", "detail": cur.detail}
    if not cur.stdout.strip():
        return {"ok": True, "released": False, "detail": "no lock present"}
    try:
        held = json.loads(cur.stdout.strip())
    except Exception:
        held = {}
    if held.get("sid") != session_id():
        return {"ok": False, "released": False, "error": "not_owner",
                "detail": "refusing to release a lock we don't own", "holder_sid": held.get("sid")}
    rm = _run_remote(f"rm -f {qp}")
    return {"ok": rm.ok, "released": rm.ok, "detail": rm.detail}


def _lock_is_stale(held: dict[str, Any]) -> bool:
    """A lock is stale if acquired_at + ttl_seconds < now. Unparseable timestamp => treat as NOT stale
    (conservative: never steal a lock we can't reason about)."""
    ts = held.get("acquired_at")
    ttl = held.get("ttl_seconds", 60)
    if not ts:
        return False
    try:
        acquired = datetime.fromisoformat(ts)
        if acquired.tzinfo is None:
            acquired = acquired.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - acquired.astimezone(timezone.utc)).total_seconds()
        return age > float(ttl)
    except Exception:
        return False


# --------------------------------------------------------------------------------------------------
# 4. Handshake + domain proposal (the content M approves; NOT auto-sent)
# --------------------------------------------------------------------------------------------------

def version_handshake_message() -> dict[str, Any]:
    """The 'I support v3' version-handshake envelope. Built, not sent, per the brief the cutover is
    M-gated; this is the message mongoose would post once M approves activation."""
    return build_envelope(
        topic="version-handshake",
        body=(f"mongoose supports protocol v{PROTOCOL_VERSION}: can produce/parse the envelope, take "
              f"Pi-side locks over SSH (atomic create, TTL, stale-reclaim, release-only-own), and honour "
              f"panel-safety + verify-live-state + relayed-approval-is-not-approval."),
        to="all",
        needs_ack=True,
    )


def proposed_domains() -> list[dict[str, str]]:
    """The functional domains mongoose PROPOSES to own (the brief says propose, don't assume). These are
    suggestions for M, chosen to sit cleanly inside mongoose's namespace (/config/mongoose/*, mongoose_*
    helpers) and not overlap chat's or code's ownership."""
    return [
        {"domain": "household-brief-orchestration",
         "why": "mongoose already composes the daily brief from HA state; owning its schedule + sources "
                "fits its Mac-side brain, no shared-file writes needed."},
        {"domain": "mongoose-namespace-automations",
         "why": "logic authored under /config/mongoose/*.yaml driving mongoose_* helpers, fully inside "
                "mongoose's namespace, no overlap with chat/code."},
        {"domain": "cross-agent-reconciliation-watch",
         "why": "read-only watcher that verifies live HA state against the bus claims and flags drift "
                "(verify-before-summarise), surfacing conflicts for M rather than acting."},
    ]


def supports_v3_report() -> dict[str, Any]:
    """A single self-check mongoose can show M / use in a handshake decision: do I have the capability to
    speak v3, and is the Pi transport ready? Pure read, changes nothing."""
    t = transport_status()
    last_verified = None
    try:
        from ..db import get_app_state
        last_verified = get_app_state("pi_ssh_last_verified")
    except Exception:
        pass
    return {
        "protocol_version": PROTOCOL_VERSION,
        "can_build_envelope": True,
        "can_parse_envelope": True,
        "can_take_pi_locks": t["configured"],
        "pi_transport": t,
        "pi_ssh_last_verified": last_verified,
        "panel_safety_enforced": True,
        "relayed_approval_trusted": relayed_approval_is_trustworthy({"m_approval_status": "approved"}),
        "live_cutover_enabled": bool(getattr(get_settings(), "protocol_v3_enabled", False)),
        "handshake": version_handshake_message(),
        "proposed_domains": proposed_domains(),
        "note": ("Capability only. v3 is a proposal; nothing is live until M sets PROTOCOL_V3_ENABLED "
                 "and the coordinated handshake completes."),
    }


# --------------------------------------------------------------------------------------------------
# 5. Handshake connectivity test (read-only; for setup) , verifies we can reach the Pi and SEE the
#    other agents, WITHOUT writing anything or going live.
# --------------------------------------------------------------------------------------------------

def handshake_test() -> dict[str, Any]:
    """A read-only dry-run of joining the protocol, for a delightful setup 'Test connection' button. It
    NEVER writes to the bus, takes no locks, and does not go live, it just checks, step by step, that
    mongoose could participate, and reports each step warmly so M can see exactly where (if anywhere) it
    stops. Steps: (1) Pi reachable over SSH; (2) shared bus file present + readable; (3) the other agents
    (chat/code) are visible on the bus. Always returns a structured result; never raises."""
    s = get_settings()
    cfg = (getattr(s, "pi_config_dir", "/config") or "/config").rstrip("/")
    steps: list[dict[str, Any]] = []

    # Step 1: can we reach the Pi at all?
    t = transport_status()
    if not t["configured"]:
        steps.append({"step": "Reach the Pi", "ok": False, "skippable_reason": "not_configured",
                      "detail": "Add the Pi address first (PI_SSH_HOST), then test."})
        return {"ok": False, "ready": False, "steps": steps,
                "summary": "Not set up yet, add your Pi's SSH address and try again."}
    if not t["reachable"]:
        # Build the EXACT command that actually works: ssh-copy-id with mongoose's own key (-i), no
        # sudo. A bare `ssh-copy-id <host>` fails with "No identities found" when the user has no
        # default ~/.ssh key, and `sudo ssh-copy-id` looks in root's home (wrong) , both of which the
        # user hit. ensure_ssh_key() creates the key first so -i points at a real file.
        try:
            keyinfo = ensure_ssh_key()
            pub = keyinfo.get("key_path", "") + ".pub" if keyinfo.get("ok") else ""
        except Exception:
            pub = ""
        # If the key couldn't be generated this instant (e.g. ssh-keygen momentarily unavailable),
        # still point at where mongoose's key lives, so the command is correct rather than the bare
        # form that causes "No identities found".
        if not pub:
            pub = _default_key_path() + ".pub"
        host = (getattr(get_settings(), "pi_ssh_host", "") or "<pi-host>").strip() or "<pi-host>"
        port = int(getattr(get_settings(), "pi_ssh_port", 22) or 22)
        cmd = _manual_copy_cmd(pub, host, port)
        reason = (t.get("detail") or "").lower()
        if "permission denied" in reason and "publickey" in reason:
            # SSH is reachable and running; the key was specifically presented and REJECTED. The
            # single most common cause: ssh-copy-id appended the key, but the Pi's ~/.ssh or
            # authorized_keys permissions are too open, so sshd's StrictModes silently distrusts
            # the file and falls back to a password prompt -- which reads as "ssh-copy-id worked
            # but the follow-up ssh still asks for a password", not as SSH being unreachable.
            steps.append({"step": "Reach the Pi", "ok": False,
                          "detail": f"The Pi rejected mongoose's key ({t.get('detail','')}). SSH is "
                                    "running and reachable, this is specifically a key-auth problem, "
                                    "not a connectivity one. The most common cause: the key was copied "
                                    "but the permissions on the Pi don't pass sshd's strict checks, so "
                                    "it silently falls back to asking for a password. Fix on the Pi "
                                    "(SSH in with the password once): "
                                    "`chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys` "
                                    "(for root: `sudo chmod 700 /root/.ssh && sudo chmod 600 "
                                    "/root/.ssh/authorized_keys`). Also worth a quick check: "
                                    "`sudo sshd -T | grep -i permitrootlogin` on the Pi, if it says "
                                    "\"no\", root can never log in over SSH even with a correct key, "
                                    "and PI_SSH_HOST/the copy command should target a normal user "
                                    "instead of root.",
                          "manual_command": cmd,
                          "likely_cause": "authorized_keys_permissions_or_root_login_disabled"})
            return {"ok": False, "ready": False, "steps": steps,
                    "summary": "The Pi is reachable and SSH is running, but it rejected mongoose's "
                               "key, almost always a permissions issue on the Pi's ~/.ssh, or root "
                               "SSH login being disabled. See the detail above for the exact fix."}
        steps.append({"step": "Reach the Pi", "ok": False,
                      "detail": f"Couldn't reach the Pi over SSH ({t.get('detail','')}). "
                                "Three things to check: (1) SSH is enabled on the Pi "
                                "(raspi-config, Interface Options, SSH, or `sudo systemctl enable --now ssh`); "
                                f"(2) authorise mongoose's key, run this ONCE (no sudo): `{cmd}` "
                                "(you'll enter the Pi password just this once); "
                                f"(3) confirm it works: `ssh -i {pub} {host}` should log in without a password.",
                      "manual_command": cmd,
                      "warning": "Do NOT use sudo with ssh-copy-id, it looks in the wrong home folder. "
                                 "And don't run a bare `ssh-copy-id <host>`; use the -i form above so it "
                                 "uses mongoose's key (you may have no default ~/.ssh key, which is what "
                                 "causes 'No identities found')."})
        return {"ok": False, "ready": False, "steps": steps,
                "summary": "Couldn't reach the Pi over SSH. Authorise mongoose's key once with: "
                           + cmd + "  (no sudo), then test with: ssh -i " + (pub or "<key>") + " " + host + "."}
    steps.append({"step": "Reach the Pi", "ok": True, "detail": "Connected over SSH."})
    try:
        from ..db import set_app_state
        from datetime import datetime, timezone
        set_app_state("pi_ssh_last_verified", datetime.now(timezone.utc).isoformat())
    except Exception:
        pass

    # Step 2: is the shared bus present + readable? (read-only)
    bus_path = f"{cfg}/claude_bus.yaml"
    qb = shlex.quote(bus_path)
    tail = _run_remote(f"tail -c 4000 {qb} 2>/dev/null || true")
    bus_text = tail.stdout if tail.ok else ""
    bus_present = bool(bus_text.strip())
    if not bus_present:
        steps.append({"step": "Find the shared bus", "ok": True, "soft": True,
                      "detail": "No claude_bus.yaml yet, normal before v3 goes live. mongoose will "
                                "join it at cutover."})
        return {"ok": True, "ready": True, "bus_present": False, "steps": steps,
                "summary": "Pi reachable and ready. The shared bus isn't live yet (that's expected "
                           "before cutover), mongoose is good to go when you are."}
    steps.append({"step": "Find the shared bus", "ok": True, "detail": "Read the shared bus (tail)."})

    # Step 3: are the other agents visible on the bus? (read-only scan of the tail)
    seen = sorted({a for a in ("chat", "code") if f"from: {a}" in bus_text or f"from:{a}" in bus_text
                   or f'"{a}"' in bus_text})
    if seen:
        who = " and ".join(seen)
        steps.append({"step": "See the other agents", "ok": True,
                      "detail": f"Saw recent activity from {who} on the bus."})
        summary = f"All good, Pi reachable, bus live, and {who} {'are' if len(seen) > 1 else 'is'} there."
    else:
        steps.append({"step": "See the other agents", "ok": True, "soft": True,
                      "detail": "Bus is present but no recent chat/code messages in the tail, they may "
                                "just be quiet right now."})
        summary = "Pi reachable and the bus is live; the other agents are quiet right now, which is fine."
    return {"ok": True, "ready": True, "bus_present": True, "agents_seen": seen,
            "steps": steps, "summary": summary}


# --------------------------------------------------------------------------------------------------
# 6. Delightful SSH setup with key automation. The goal: M types the Pi address (and a password ONCE),
#    and mongoose handles keygen + ssh-copy-id + verification, so passwordless SSH "just works" after.
# --------------------------------------------------------------------------------------------------

def _default_key_path() -> str:
    """Where mongoose keeps its dedicated Pi key (separate from the user's personal keys, so we never
    touch ~/.ssh/id_rsa). Lives under the mongoose data dir."""
    import os
    base = os.path.expanduser(os.getenv("MONGOOSE_DATA_DIR", "~/.mongoose/data"))
    return os.path.join(base, "ssh", "mongoose_pi_ed25519")


def ensure_ssh_key() -> dict[str, Any]:
    """Make sure mongoose has its own SSH keypair for the Pi. Idempotent: if the key already exists it's
    reused. Uses ed25519 (modern, short, no passphrase so it can be used unattended, this is a
    machine-to-LAN-device key, scoped to the Pi). Returns the public key text so the next step can
    install it."""
    import os
    import subprocess
    key_path = _default_key_path()
    pub_path = key_path + ".pub"
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    if os.path.exists(key_path) and os.path.exists(pub_path):
        with open(pub_path) as f:
            return {"ok": True, "created": False, "key_path": key_path,
                    "public_key": f.read().strip(), "detail": "Reusing mongoose's existing Pi key."}
    try:
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path,
                        "-C", "mongoose-pi-coordination"],
                       capture_output=True, text=True, timeout=20, check=True)
    except FileNotFoundError:
        return {"ok": False, "error": "ssh-keygen not found on this machine."}
    except subprocess.CalledProcessError as exc:
        return {"ok": False, "error": f"ssh-keygen failed: {exc.stderr.strip()[:160]}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"ssh-keygen error: {exc}"}
    os.chmod(key_path, 0o600)
    with open(pub_path) as f:
        return {"ok": True, "created": True, "key_path": key_path,
                "public_key": f.read().strip(), "detail": "Created a fresh ed25519 key for the Pi."}


def authorize_key_on_pi(password: str | None = None, *, host: str | None = None,
                        port: int | None = None) -> dict[str, Any]:
    """Install mongoose's public key onto the Pi's authorized_keys so future SSH is passwordless. This
    is the one step that needs the Pi password, ONCE. We prefer ssh-copy-id; if a password is supplied
    we pipe it via sshpass when available, otherwise we return a clear instruction for the user to run
    ssh-copy-id themselves (we never store the password). After this, _ssh_base_cmd's key auth works.
    Persists PI_SSH_KEY so the transport uses mongoose's dedicated key."""
    import shutil
    import subprocess
    s = get_settings()
    pi_host = (host or getattr(s, "pi_ssh_host", "") or "").strip()
    if not pi_host:
        return {"ok": False, "error": "No Pi host set yet. Add the Pi's SSH address first."}
    pi_port = int(port or getattr(s, "pi_ssh_port", 22) or 22)

    keyinfo = ensure_ssh_key()
    if not keyinfo.get("ok"):
        return keyinfo
    key_path = keyinfo["key_path"]
    pub_path = key_path + ".pub"

    copy_id = shutil.which("ssh-copy-id")
    sshpass = shutil.which("sshpass")
    port_args = ["-p", str(pi_port)] if pi_port != 22 else []

    # Path A: password supplied + sshpass available -> fully automated, no prompt.
    if password and sshpass and copy_id:
        try:
            proc = subprocess.run(
                [sshpass, "-p", password, copy_id, "-i", pub_path,
                 "-o", "StrictHostKeyChecking=accept-new"] + port_args + [pi_host],
                capture_output=True, text=True, timeout=30)
            ok = proc.returncode == 0
            detail = proc.stderr.strip()[-200:] if not ok else "Key installed on the Pi."
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"ssh-copy-id failed: {exc}"}
        if ok:
            _persist_pi_key(key_path)
        return {"ok": ok, "method": "automated", "detail": detail, "key_path": key_path}

    # Path B: ssh-copy-id present but no password/sshpass -> run it; it will prompt in the terminal once.
    if copy_id and not password:
        try:
            proc = subprocess.run(
                [copy_id, "-i", pub_path, "-o", "StrictHostKeyChecking=accept-new"]
                + port_args + [pi_host],
                capture_output=True, text=True, timeout=60)
            ok = proc.returncode == 0
            if ok:
                _persist_pi_key(key_path)
            return {"ok": ok, "method": "ssh-copy-id",
                    "detail": "Key installed." if ok else proc.stderr.strip()[-200:],
                    "key_path": key_path}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "ssh-copy-id timed out (it may have been waiting for a "
                    "password). Run it yourself once: " + _manual_copy_cmd(pub_path, pi_host, pi_port)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"ssh-copy-id error: {exc}"}

    # Path C: no automation available -> give the exact one-time command (never store the password).
    cmd = _manual_copy_cmd(pub_path, pi_host, pi_port)
    warn = ("IMPORTANT: run EXACTLY this command. Do NOT add sudo (it looks in root's home and fails), "
            "and do NOT run a bare `ssh-copy-id " + pi_host + "` (that gives 'No identities found' "
            "because it looks for a default ~/.ssh key you may not have). The -i flag points at "
            "mongoose's own key, which is why this works.")
    out = {"ok": False, "needs_manual": True,
           "public_key": keyinfo["public_key"],
           "manual_command": cmd,
           "warning": warn,
           "detail": "Run this ONE command in Terminal to authorise mongoose's key (you'll enter the "
                     "Pi password just this once); after that SSH is passwordless. " + warn}
    # If a password WAS supplied but sshpass is missing, that's the most common reason we landed here.
    # Offer the one-liner to install sshpass so next time it's fully automatic.
    if password and not sshpass:
        out["needs_sshpass"] = True
        out["install_command"] = "brew install hudochenkov/sshpass/sshpass"
        out["detail"] = ("To make this fully automatic (no manual command), install sshpass once with "
                         "the command below, then click Set up again. Or run the manual command shown.")
    return out


def _manual_copy_cmd(pub_path: str, host: str, port: int) -> str:
    p = f"-p {port} " if port != 22 else ""
    return f"ssh-copy-id {p}-i {pub_path} {host}"


def _persist_pi_creds(*, host: str | None, port: int | None, password: str) -> None:
    """Persist keyless-password Pi credentials to .env and clear any key so the transport uses the
    password path. Plaintext on disk in .env, same as IMAP_PASSWORD; we say so honestly in the UI."""
    try:
        import os
        from ..setup_wizard import write_env
        updates: dict[str, str] = {"PI_SSH_PASSWORD": password, "PI_SSH_KEY": ""}
        if host:
            updates["PI_SSH_HOST"] = host.strip()
        if port:
            updates["PI_SSH_PORT"] = str(port)
        write_env(updates)
        for k, v in updates.items():
            os.environ[k] = v
        import mongoose.settings as _S
        if hasattr(_S, "_settings"):
            _S._settings = None
        if hasattr(_S.get_settings, "cache_clear"):
            _S.get_settings.cache_clear()
    except Exception:
        pass


def setup_pi_ssh_password(password: str, *, host: str | None = None,
                          port: int | None = None) -> dict[str, Any]:
    """Delightful KEYLESS setup: the user gives just the Pi's SSH address (user@host) and password,
    and mongoose connects with that, no key generation, no ssh-copy-id, no key files. It saves the
    credentials, then verifies the connection actually works with a harmless probe, reporting each step
    warmly. Honest about two things: it needs `sshpass` on the Mac, and the password lives in .env in
    plaintext (same as the IMAP password), a key is more secure if you ever want it."""
    import shutil
    steps: list[dict[str, Any]] = []
    s0 = get_settings()
    effective_host = (host or getattr(s0, "pi_ssh_host", "") or "").strip()
    if not effective_host:
        return {"ok": False, "ready": False,
                "summary": "Add the Pi's SSH address first (like pi@raspberrypi.local), then the password."}
    if not password:
        return {"ok": False, "ready": False,
                "summary": "Add the Pi password to set up keyless mode."}

    # sshpass is required for non-interactive password auth.
    if not shutil.which("sshpass"):
        steps.append({"step": "Check for sshpass", "ok": False,
                      "detail": "sshpass isn't installed on this Mac."})
        return {"ok": False, "ready": False, "needs_sshpass": True, "steps": steps,
                "install_command": "brew install sshpass",
                "summary": "Keyless password mode needs a small helper called sshpass. Install it once "
                           "with: brew install sshpass, then set up again. (Or use key mode, which "
                           "needs no extra tools.)"}
    steps.append({"step": "Check for sshpass", "ok": True, "detail": "sshpass is available."})

    # Persist creds (clears any key so the password path is used) and verify.
    _persist_pi_creds(host=host, port=port, password=password)
    steps.append({"step": "Save the connection", "ok": True,
                  "detail": "Saved the Pi address + password (kept in .env, like your other passwords)."})

    t = transport_status()
    if t.get("reachable"):
        steps.append({"step": "Connect to the Pi", "ok": True, "detail": "Connected with the password."})
        greet = post_handshake_when_open(force=True)
        if greet.get("posted"):
            steps.append({"step": "Greet the other agents", "ok": True,
                          "detail": "Posted mongoose's handshake to chat and code."})
        return {"ok": True, "ready": True, "mode": "password", "steps": steps,
                "greeted": greet.get("posted", False),
                "summary": "Done, mongoose connects to the Pi with just the username and password. No "
                           "keys needed. (A key would be a bit more secure if you ever want one.)"}
    steps.append({"step": "Connect to the Pi", "ok": False,
                  "detail": t.get("detail", "couldn't connect with that password.")})
    return {"ok": False, "ready": False, "mode": "password", "steps": steps,
            "summary": "Saved, but the test connection failed. Check the address and password, and that "
                       "the Pi has SSH enabled and allows password login."}


def _persist_pi_key(key_path: str) -> None:
    """Point the transport at mongoose's dedicated key by writing PI_SSH_KEY to .env."""
    try:
        from ..setup_wizard import write_env
        write_env({"PI_SSH_KEY": key_path})
        import os
        os.environ["PI_SSH_KEY"] = key_path
        import mongoose.settings as _S
        if hasattr(_S, "_settings"):
            _S._settings = None
        if hasattr(_S.get_settings, "cache_clear"):
            _S.get_settings.cache_clear()
    except Exception:
        pass


def setup_pi_ssh(password: str | None = None, *, host: str | None = None,
                 port: int | None = None) -> dict[str, Any]:
    """The delightful one-call Pi SSH setup: persist host, ensure a key, authorise it on the Pi, and
    verify passwordless access works, reporting each step warmly. If it's already passwordless, it
    short-circuits and celebrates. Returns step-by-step results for the cockpit to render."""
    steps: list[dict[str, Any]] = []
    # Resolve the host first: if there's none (neither passed nor saved), say so plainly up front.
    s0 = get_settings()
    effective_host = (host or getattr(s0, "pi_ssh_host", "") or "").strip()
    if not effective_host:
        steps.append({"step": "Find the Pi", "ok": False,
                      "detail": "No Pi SSH address yet."})
        return {"ok": False, "ready": False, "steps": steps,
                "summary": "Add the Pi's SSH address first (like pi@raspberrypi.local), then set up SSH."}
    # Persist host/port if given.
    if host:
        try:
            import os
            from ..setup_wizard import write_env
            updates = {"PI_SSH_HOST": host.strip()}
            if port:
                updates["PI_SSH_PORT"] = str(port)
            write_env(updates)
            for k, v in updates.items():
                os.environ[k] = v
            import mongoose.settings as _S
            if hasattr(_S, "_settings"):
                _S._settings = None
            if hasattr(_S.get_settings, "cache_clear"):
                _S.get_settings.cache_clear()
        except Exception:
            pass

    # Already passwordless?
    t = transport_status()
    if t.get("reachable"):
        steps.append({"step": "Connect to the Pi", "ok": True,
                      "detail": "Already passwordless, nothing to do."})
        return {"ok": True, "ready": True, "already": True, "steps": steps,
                "summary": "Already set up, mongoose can reach the Pi over SSH without a password."}

    # Ensure key.
    k = ensure_ssh_key()
    if not k.get("ok"):
        steps.append({"step": "Prepare a key", "ok": False, "detail": k.get("error", "")})
        return {"ok": False, "ready": False, "steps": steps,
                "summary": "Couldn't prepare an SSH key: " + k.get("error", "")}
    steps.append({"step": "Prepare a key", "ok": True,
                  "detail": k.get("detail", "Key ready.")})

    # Authorise it.
    a = authorize_key_on_pi(password=password, host=host, port=port)
    if not a.get("ok"):
        if a.get("needs_manual"):
            steps.append({"step": "Authorise the key on the Pi", "ok": False, "soft": True,
                          "detail": a.get("detail", ""), "manual_command": a.get("manual_command")})
            return {"ok": False, "ready": False, "needs_manual": True,
                    "manual_command": a.get("manual_command"), "steps": steps,
                    "summary": "One manual step: run the shown command once to authorise mongoose's "
                               "key (you'll enter the Pi password just once)."}
        steps.append({"step": "Authorise the key on the Pi", "ok": False, "detail": a.get("error", a.get("detail", ""))})
        return {"ok": False, "ready": False, "steps": steps,
                "summary": "Couldn't authorise the key: " + a.get("error", a.get("detail", ""))}
    steps.append({"step": "Authorise the key on the Pi", "ok": True,
                  "detail": "mongoose's key is now trusted by the Pi."})

    # Verify.
    t2 = transport_status()
    if t2.get("reachable"):
        steps.append({"step": "Verify passwordless SSH", "ok": True, "detail": "Confirmed."})
        # Greet peers immediately if v3 is already enabled (otherwise this is a gated no-op that will
        # fire automatically once M turns v3 on and the scheduler next ticks).
        greet = post_handshake_when_open(force=True)
        if greet.get("posted"):
            steps.append({"step": "Greet the other agents", "ok": True,
                          "detail": "Posted mongoose's handshake to chat and code."})
        return {"ok": True, "ready": True, "steps": steps, "greeted": greet.get("posted", False),
                "summary": "Done, mongoose can now reach the Pi over SSH without a password."}
    steps.append({"step": "Verify passwordless SSH", "ok": False,
                  "detail": "Key installed but the test connection still failed; check the Pi is on "
                            "and SSH is enabled."})
    return {"ok": False, "ready": False, "steps": steps,
            "summary": "Key installed, but the verify step couldn't connect. Check the Pi is on and "
                       "SSH is enabled, then test again."}


# --------------------------------------------------------------------------------------------------
# 7. Auto-handshake on connection open. When v3 is enabled and the Pi becomes reachable, mongoose posts
#    its version-handshake to peers automatically , so the moment the link is open, peers know it's
#    here. Fires ONLY on the unreachable->reachable transition (idempotent), and only when M has
#    enabled v3 (it never flips the live protocol itself).
# --------------------------------------------------------------------------------------------------

def _append_to_bus(envelope: dict[str, Any]) -> dict[str, Any]:
    """Append one envelope to the shared bus on the Pi, under a short-lived lock. Writes YAML by
    appending a list item. Read-only-safe: takes the lock, appends, releases. Returns ok/detail."""
    import json as _json
    import shlex as _shlex
    s = get_settings()
    cfg = (getattr(s, "pi_config_dir", "/config") or "/config").rstrip("/")
    # PER-AGENT FILE DESIGN: mongoose appends ONLY to its OWN file, never the shared claude_bus.yaml, so
    # it can't be clobbered by (and can't clobber) another system's write. The shared file is a
    # multi-writer race , notably chat's HA MCP save_config_file is a full-file rewrite. Peers read
    # claude_bus_mongoose.yaml for mongoose's messages/acks/handshake. The lock here only guards
    # mongoose's own concurrent posts (heartbeat vs ack), not cross-agent contention.
    bus = f"{cfg}/claude_bus_mongoose.yaml"
    lock = acquire_lock("claude_bus_mongoose.yaml", ttl_seconds=30)
    if not lock.get("ok"):
        return {"ok": False, "detail": f"couldn't take the bus lock: {lock.get('detail','')}"}
    try:
        # Append as a one-line JSON list item (valid YAML). Append-only, single writer.
        line = "- " + _json.dumps(envelope, ensure_ascii=False)
        q = _shlex.quote(line)
        qb = _shlex.quote(bus)
        res = _run_remote(f"printf '%s\\n' {q} >> {qb}")
        return {"ok": res.ok, "detail": res.detail or ("appended" if res.ok else res.stderr[:160])}
    finally:
        release_lock("claude_bus_mongoose.yaml")


def post_handshake_when_open(*, force: bool = False) -> dict[str, Any]:
    """Post mongoose's version-handshake to peers IF the connection is open and v3 is enabled. Designed
    to be called repeatedly (e.g. by the scheduler or right after SSH setup succeeds): it only actually
    posts on the transition into 'reachable', tracked via app_state, so peers get exactly one greeting
    when the link opens, not a flood. `force` re-posts regardless of the transition flag.

    Strictly M-gated: if protocol_v3_enabled is False, this does NOTHING (returns gated). It never flips
    the protocol on; it only greets once M has turned v3 on AND the Pi is reachable."""
    from ..db import get_app_state, set_app_state
    s = get_settings()
    enabled = bool(getattr(s, "protocol_v3_enabled", False))
    if not enabled:
        return {"ok": True, "posted": False, "reason": "gated",
                "detail": "v3 is not enabled; mongoose won't post a handshake until M turns it on."}

    t = transport_status()
    reachable = bool(t.get("reachable"))
    prev_key = "v3_last_reachable"
    prev = (get_app_state(prev_key) or "") == "1"
    # update the remembered state regardless
    set_app_state(prev_key, "1" if reachable else "0")

    if not reachable:
        return {"ok": True, "posted": False, "reason": "unreachable",
                "detail": "Pi not reachable; will greet peers automatically when the link opens."}

    # Only greet on the transition (unreachable -> reachable), unless forced.
    just_opened = reachable and not prev
    if not (just_opened or force):
        return {"ok": True, "posted": False, "reason": "already_open",
                "detail": "Link was already open; handshake was posted earlier."}

    env = version_handshake_message()
    res = _append_to_bus(env)
    if res.get("ok"):
        set_app_state("v3_last_handshake_id", env.get("msg_id", ""))
        return {"ok": True, "posted": True, "msg_id": env.get("msg_id"),
                "detail": "Connection open, posted mongoose's handshake to chat and code."}
    return {"ok": False, "posted": False, "detail": res.get("detail", "append failed")}


# --- Remote binary read (voicemail audio that arrives via the Pi/HA staging path) ------------------
def _config_dir() -> str:
    """The Pi's Home Assistant /config directory (PI_CONFIG_DIR), trailing slash stripped."""
    return (getattr(get_settings(), "pi_config_dir", "/config") or "/config").rstrip("/")


def _remote_path(rel_or_abs: str) -> str:
    """Resolve a shared path to the Pi config filesystem. An absolute path is used as-is; a bare name
    is taken relative to /config. Never interpreted as a Mac-local path (the Pi is the source of truth)."""
    p = (rel_or_abs or "").strip()
    if p.startswith("/"):
        return p
    return f"{_config_dir()}/{p}"


def read_remote_file_b64(rel_or_abs: str, *, max_bytes: int = 25 * 1024 * 1024) -> dict[str, Any]:
    """Read a binary file from the Pi as base64, never from a Mac-local path. Used for voicemail audio
    that arrives through the HA/Pi staging path before IMAP has stored a local attachment. Uses the same
    SSH transport and /config-relative resolver as the bus client. Never raises."""
    path = _remote_path(rel_or_abs)
    qp = shlex.quote(path)
    res = _run_remote(f"test -f {qp} && wc -c < {qp} && base64 {qp}", timeout=20)
    if not res.configured:
        return {"ok": False, "configured": False, "error": "not_configured", "detail": res.detail}
    if not res.ok:
        return {"ok": False, "configured": True, "error": "unreadable",
                "detail": res.detail or res.stderr[:200]}
    lines = (res.stdout or "").splitlines()
    if not lines:
        return {"ok": False, "configured": True, "error": "empty_response"}
    try:
        size = int(lines[0].strip())
    except Exception:
        size = 0
    if size > max_bytes:
        return {"ok": False, "configured": True, "error": "too_large",
                "size_bytes": size, "max_bytes": max_bytes}
    data_b64 = "".join(lines[1:]).strip()
    if not data_b64:
        return {"ok": False, "configured": True, "error": "no_payload", "size_bytes": size}
    return {"ok": True, "configured": True, "path": path, "size_bytes": size, "payload_b64": data_b64}
