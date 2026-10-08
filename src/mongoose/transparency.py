from __future__ import annotations

import os
from typing import Any

# The system map: a single, honest answer to "what IS this?" for a non-developer.
#
# The receipts ledger answers "what did Mongoose DO?". This answers "what IS Mongoose,
# right now?": what's connected, what each surface (this Mac, your phone) can actually
# see, WHERE your data physically lives, and, most importantly for a privacy-first
# assistant, WHAT LEAVES this machine.
#
# Everything here is COMPUTED from real state, not asserted. Row counts are real counts.
# The storage path and size are the real file. The egress list reflects actual settings
# (what's gated off, what you've turned on). The point is a person can verify the privacy
# claim instead of being asked to trust a slogan.


def _data_categories() -> list[dict[str, Any]]:
    """The real categories of personal data held, with ACTUAL row counts. Honest about
    what Mongoose has ingested, no more, no less."""
    from .db import connect
    # (table, human label, what it is) , only the content tables a person cares about.
    spec = [
        ("email_messages", "Email", "messages synced from connected mail accounts"),
        ("whatsapp_messages", "WhatsApp", "messages from your linked WhatsApp"),
        ("events", "Calendar", "events from connected calendars"),
        ("tasks", "Tasks", "to-dos from connected task lists"),
        ("files", "Files", "documents indexed from chosen folders"),
        ("notifications", "Notifications", "phone notifications shared with Mongoose"),
        ("memory_facts", "Memory", "facts Mongoose has remembered about you"),
        # Mongoose keeps ONE continuous conversation, so a count of conversation rows is meaningless
        # (it's ~1). What a person actually cares about is how much chat history is stored, i.e. the
        # number of messages. Handled specially below.
    ]
    out = []
    with connect() as con:
        existing = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table, label, what in spec:
            n = 0
            if table in existing:
                try:
                    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except Exception:
                    n = 0
            out.append({"category": label, "what": what, "count": n,
                        "present": n > 0})
        # Chat history as message count in the single continuous conversation. Scoped to
        # DEFAULT_CONVERSATION_ID specifically, matching what the label claims: previously this
        # counted every row in the whole table regardless of conversation_id, which is a
        # different, larger number the moment any other conversation_id is ever used.
        msg_n = 0
        if "messages" in existing:
            try:
                from .services.memory import DEFAULT_CONVERSATION_ID
                msg_n = con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                                    (DEFAULT_CONVERSATION_ID,)).fetchone()[0]
            except Exception:
                msg_n = 0
        out.append({"category": "Chat history",
                    "what": "messages in your one continuous conversation with Mongoose",
                    "count": msg_n, "present": msg_n > 0})
        # Guest devices chat in their OWN isolated threads (conversation_id guest:<device_id>) in
        # this same table. Report them as their own category: now that the main count is
        # correctly scoped, omitting these would silently hide real stored data from the summary.
        guest_n = 0
        guest_convos = 0
        if "messages" in existing:
            try:
                row = con.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT conversation_id) FROM messages "
                    "WHERE conversation_id LIKE 'guest:%'").fetchone()
                guest_n, guest_convos = int(row[0]), int(row[1])
            except Exception:
                guest_n, guest_convos = 0, 0
        out.append({"category": "Guest chat history",
                    "what": (f"messages across {guest_convos} isolated guest conversation"
                             f"{'s' if guest_convos != 1 else ''} (each paired guest device has "
                             "its own thread, separate from yours)"),
                    "count": guest_n, "present": guest_n > 0})
    return out


def _disk_encryption() -> dict[str, Any]:
    """Whether the data sits on an encrypted volume. This is the HONEST answer to at-rest
    risk: Mongoose does NOT encrypt its own SQLite file (a local-first headless service has
    nowhere safe to keep a DB key, so app-level encryption would be theatre, the key would
    sit next to the data). The right layer is full-disk encryption (FileVault on macOS). We
    detect it and tell the truth rather than implying safety the app hasn't provided."""
    import subprocess
    try:
        # macOS: fdesetup status. Don't assume; check.
        out = subprocess.run(["fdesetup", "status"], capture_output=True, text=True, timeout=5)
        text = (out.stdout or "") + (out.stderr or "")
        if "FileVault is On" in text:
            return {"encrypted": True, "mechanism": "FileVault (full-disk)",
                    "detail": "Your disk is encrypted with FileVault, so the data file and "
                              "secrets are protected at rest if this machine is lost or stolen."}
        if "FileVault is Off" in text:
            return {"encrypted": False, "mechanism": "none",
                    "detail": "FileVault is OFF. Mongoose's data is an UNENCRYPTED file on "
                              "this disk; a stolen machine or a copied data folder exposes "
                              "your email, messages, and tokens. Turn on FileVault "
                              "(System Settings > Privacy & Security > FileVault)."}
    except Exception:
        pass
    return {"encrypted": None, "mechanism": "unknown",
            "detail": "Could not determine disk encryption. Mongoose does NOT encrypt its "
                      "own data file, so at-rest protection depends on full-disk encryption "
                      "(FileVault on macOS). Confirm it's on if this machine holds sensitive data."}


def _storage() -> dict[str, Any]:
    """Where the data physically lives, and the HONEST at-rest exposure. One local file,
    named plainly, with its real size, plus whether the disk is actually encrypted."""
    from .settings import get_settings
    s = get_settings()
    path = str(s.db_path)
    size = None
    try:
        if os.path.exists(s.db_path):
            size = os.path.getsize(s.db_path)
    except Exception:
        size = None
    human = None
    if size is not None:
        mb = size / (1024 * 1024)
        human = f"{mb:.1f} MB" if mb >= 1 else f"{size / 1024:.0f} KB"
    enc = _disk_encryption()
    return {
        "location": path,
        "size": human,
        "where": "on this machine",
        "encrypted_at_rest": enc["encrypted"],
        "encryption_mechanism": enc["mechanism"],
        "at_rest_note": enc["detail"],
        "note": "Your data is a single SQLite file on this computer. You can see it, back "
                "it up, or delete it yourself. Mongoose does not encrypt this file itself, "
                "at-rest protection comes from full-disk encryption (see above).",
    }


def _surfaces() -> list[dict[str, Any]]:
    """Each surface (this Mac, paired devices) and what it can actually see/do. The Mac is
    the brain; phones/tablets are scoped surfaces."""
    surfaces = [{
        "name": "This Mac",
        "role": "brain",
        "can": ["runs the on-device model", "holds all your data", "does the thinking"],
        "status": "active",
    }]
    try:
        from .devices import list_devices
        for d in list_devices():
            if d.get("revoked"):
                continue
            caps = [c.get("label") for c in (d.get("capability_status") or [])
                    if c.get("state") == "enabled"]
            fresh = (d.get("freshness") or {}).get("detail", "")
            surfaces.append({
                "name": d.get("name"),
                "role": d.get("profile") or "device",
                "can": caps or ["paired (no capabilities enabled yet)"],
                "status": fresh or "paired",
            })
    except Exception:
        pass
    return surfaces


def _egress() -> dict[str, Any]:
    """THE privacy-critical truth: what leaves this machine. Computed from real settings,
    what's gated off, and which optional connections are actually on."""
    from .settings import get_settings
    s = get_settings()

    # The local brain is the default and stays local.
    brain_local = "127.0.0.1" in (getattr(s, "ollama_base_url", "") or "") or \
                  "localhost" in (getattr(s, "ollama_base_url", "") or "")

    flows = []
    # 1) The model: local by default; cloud only if explicitly enabled.
    cloud_on = bool(getattr(s, "cloud_escalation_enabled", False))
    flows.append({
        "name": "The model (your questions + context)",
        "destination": "stays on this machine (local Ollama)" if not cloud_on else
                       f"may escalate to cloud providers ({getattr(s, 'cloud_escalation_order', '')})",
        "leaves_machine": cloud_on,
        "default": "local",
        "note": ("Cloud escalation is OFF. Your content goes only to the on-device model."
                 if not cloud_on else
                 "You have enabled cloud escalation: some requests may be sent to external "
                 "model providers."),
    })
    # 2) Calendar/email sync: outbound only if you ACTUALLY connected an account (the
    # credential file exists / a real token is present), not merely if a default path is
    # configured. Otherwise we'd cry wolf about data leaving when nothing is set up.
    has_google = False
    try:
        from .oauth import google_token_present
        has_google = bool(google_token_present())
    except Exception:
        has_google = False
    has_ms = bool(getattr(s, "ms_refresh_token", "") or getattr(s, "microsoft_refresh_token", ""))
    if not has_ms:
        try:
            from .oauth import microsoft_token_present
            has_ms = bool(microsoft_token_present())
        except Exception:
            has_ms = False
    if has_google or has_ms:
        providers = ", ".join([p for p, on in
                               [("Google", has_google), ("Microsoft", has_ms)] if on])
        flows.append({
            "name": "Calendar / email sync",
            "destination": f"{providers} (to fetch your own data)",
            "leaves_machine": True, "default": "off",
            "note": f"You connected {providers}. Mongoose contacts it to sync your own "
                    "calendar/email into the local store.",
        })
    # 3) Phone push: outbound only if ntfy points somewhere REAL (configured to a non-local
    # host). A localhost/default value isn't an external flow.
    ntfy_url = getattr(s, "ntfy_url", "") or ""
    ntfy_topic = getattr(s, "ntfy_topic", "") or ""
    ntfy_external = bool(ntfy_url and ntfy_topic
                         and "127.0.0.1" not in ntfy_url and "localhost" not in ntfy_url)
    if ntfy_external:
        flows.append({
            "name": "Phone notifications (push out)",
            "destination": f"{ntfy_url} (to alert your phone)",
            "leaves_machine": True, "default": "off",
            "note": "You enabled push, so Mongoose can send short alerts to your phone via "
                    "ntfy. It sends the alert text, not your data store.",
        })
    # 4) Web search. SUBTLE but important for honesty: even when the SearXNG INSTANCE is
    # local (the default), a web search inherently sends your QUERY out to search engines
    # on the internet. So web search is disclosed as reaching the internet whenever it's
    # enabled; we just distinguish "via your own local instance" from "via an external
    # instance" (the latter also exposes queries to whoever runs that instance).
    searxng = getattr(s, "searxng_url", "") or ""
    if searxng:
        local_instance = "127.0.0.1" in searxng or "localhost" in searxng
        if local_instance:
            dest = "search engines on the internet (routed via your own local SearXNG)"
            note = ("Web search is on. Your search terms reach search engines on the "
                    "internet, but routed through SearXNG running on THIS machine, so no "
                    "third-party server sees them first. Set MONGOOSE_SEARXNG_URL empty to "
                    "turn web search off.")
        else:
            dest = f"{searxng} and the search engines it queries"
            note = ("Web search is on and points at an EXTERNAL SearXNG instance, so your "
                    "search terms go to that server as well as to search engines.")
        flows.append({
            "name": "Web search (only when you search the web)",
            "destination": dest,
            "leaves_machine": True, "default": "on (local instance)",
            "note": note,
        })

    leaving = [f for f in flows if f["leaves_machine"]]
    # Distinguish flows that fire only on an explicit action (web search) from ones that
    # run on their own (sync). "Nothing leaves" should mean nothing leaves WITHOUT you
    # doing something, the honest, useful version of the claim.
    on_action = [f for f in leaving if "search" in f["name"].lower()]
    passive = [f for f in leaving if f not in on_action]
    if not leaving:
        summary = ("By default, nothing about your content leaves this machine. "
                   "No external connections are currently enabled.")
    elif not passive:
        summary = ("Nothing about your content leaves this machine on its own. The only "
                   "outbound path is web search, and only when you actually search the "
                   "web (your search terms then reach search engines). Everything else "
                   "stays local.")
    else:
        summary = (f"Your data stays on this machine, with {len(passive)} connection(s) you "
                   "turned on to sync your own accounts, plus web search when you use it. "
                   "Each outbound path is listed above with exactly where it sends data.")
    return {
        "flows": flows,
        "anything_leaves": bool(passive),   # 'leaves on its own' = passive flows only
        "anything_leaves_on_action": bool(on_action),
        "summary": summary,
        "brain_local": brain_local,
    }


def system_map() -> dict[str, Any]:
    """The whole picture: what Mongoose is, what it holds, who can see what, and what
    leaves. One legible answer for a non-developer. Never raises."""
    try:
        categories = _data_categories()
    except Exception:
        categories = []
    try:
        storage = _storage()
    except Exception:
        storage = {}
    try:
        surfaces = _surfaces()
    except Exception:
        surfaces = []
    try:
        egress = _egress()
    except Exception:
        egress = {"flows": [], "anything_leaves": False, "summary": ""}

    connected = [c for c in categories if c["present"]]
    return {
        "headline": "Mongoose runs on this Mac. Your data lives here. You control what it "
                    "can see and what, if anything, leaves.",
        "data_categories": categories,
        "connected_count": len(connected),
        "storage": storage,
        "surfaces": surfaces,
        "egress": egress,
    }


def format_system_map(m: dict[str, Any]) -> str:
    """Render the system map as plain text for the CLI."""
    out = [m.get("headline", ""), ""]
    out.append("WHAT MONGOOSE HOLDS (on this machine):")
    for c in m.get("data_categories", []):
        mark = f"{c['count']}" if c["present"] else "none"
        out.append(f"  {c['category']}: {mark}  ({c['what']})")
    st = m.get("storage", {})
    if st:
        out.append(f"\nWHERE IT LIVES: {st.get('location')} "
                   f"({st.get('size') or 'empty'}), {st.get('where')}")
    out.append("\nSURFACES:")
    for s in m.get("surfaces", []):
        out.append(f"  {s['name']} [{s['role']}]: {', '.join(s['can'])}, {s['status']}")
    eg = m.get("egress", {})
    out.append("\nWHAT LEAVES THIS MACHINE:")
    for f in eg.get("flows", []):
        arrow = "LEAVES" if f["leaves_machine"] else "stays local"
        out.append(f"  [{arrow}] {f['name']} -> {f['destination']}")
    out.append(f"\n{eg.get('summary', '')}")
    return "\n".join(out)
