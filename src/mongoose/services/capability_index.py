"""Capability index: Mongoose's product knowledge as searchable cards, built live from the
registries that already exist. Embeddings discover; the registry authorises; nothing here
executes anything.

Design choices, deliberately cheap:
  - Cards are built AT CALL TIME from live truth (TOOLS registry, FastAPI routes, the CLI
    parser source, capability_guide workflows). No schema, no index file, no reindex job, no
    drift: if a tool exists, its card exists.
  - Hybrid search is keyword token overlap plus the optional multilingual embedder from
    services/embeddings (cosine when the model is installed, silently absent otherwise). For
    short commands like "sync gmail" keywords beat vectors anyway.
  - gap_report() answers "can chat do everything the Cockpit can?" with numbers: chat-native
    capabilities, CLI-only commands, cockpit-only routes (heuristic token matching against
    chat tool descriptions). The number that should shrink release by release.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_WORD = re.compile(r"[a-z0-9]+")

# Polish (and other) accented letters, folded to ASCII so lingo-free queries match the alias
# keys. NFKD leaves many of these as base+combining, but some (ł) do not decompose, so an
# explicit map is the reliable path (external audit A6: naprawić/wtyczkę never matched).
_FOLD_MAP = str.maketrans({
    "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n", "ó": "o", "ś": "s", "ź": "z", "ż": "z",
})


def _fold(text: str) -> str:
    return (text or "").lower().translate(_FOLD_MAP)


def _toks(text: str) -> set[str]:
    return {w for w in _WORD.findall(_fold(text)) if len(w) > 2}


def _cli_commands() -> list[tuple[str, str]]:
    """(command, help) pairs parsed from the CLI source. Regex over add_parser is crude but
    the shapes are uniform; building the real parser would execute main()'s side effects."""
    src = Path(__file__).resolve().parents[1] / "cli.py"
    out = []
    for m in re.finditer(r'add_parser\(\s*"([^"]+)"\s*,\s*help="((?:[^"\\]|\\.)*)"', src.read_text()):
        name = m.group(1)
        if not name.startswith("_"):
            out.append((name, m.group(2)))
    return out


def build_cards() -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    # 1. Chat tools: the chat-native surface, with safety tier from the registry itself.
    from ..tools import READ_TOOLS, SENSITIVE_TOOLS, rendered_tools
    for name, spec in rendered_tools().items():
        tier = ("read" if name in READ_TOOLS
                else "sensitive" if name in SENSITIVE_TOOLS else "acting")
        cards.append({"id": f"tool:{name}", "kind": "chat_tool", "title": name,
                      "text": str(spec.get("description", "")), "tier": tier,
                      "chat_native": True})
    # 2. CLI commands.
    for cmd, help_text in _cli_commands():
        cards.append({"id": f"cli:{cmd}", "kind": "cli", "title": f"mongoose {cmd}",
                      "text": help_text, "tier": "cli", "chat_native": False})
    # 3. Routes (paths + names; the cockpit surface).
    try:
        from ..app import app
        for r in app.routes:
            path = getattr(r, "path", "")
            methods = sorted(m for m in (getattr(r, "methods", None) or set())
                             if m not in {"HEAD", "OPTIONS"})
            if not path or not methods:
                continue
            cards.append({"id": f"route:{methods[0]}:{path}", "kind": "route",
                          "title": f"{methods[0]} {path}",
                          "text": (getattr(r, "name", "") or "").replace("_", " "),
                          "tier": "route", "chat_native": False})
    except Exception:
        pass  # cards from the other sources still stand
    # 4. Guide workflows: the utterances that already work, so "I forgot the lingo" lands.
    try:
        from .capability_guide import WORKFLOWS
        for wf in WORKFLOWS:
            says = " | ".join(s.get("say", "") for s in wf.get("steps", []))
            cards.append({"id": f"workflow:{wf.get('id', wf.get('title', '?'))}",
                          "kind": "workflow", "title": str(wf.get("title", "")),
                          "text": f"{wf.get('summary', '')} {says} "
                                  + " ".join(wf.get("keywords", [])),
                          "tier": "guide", "chat_native": True})
    except Exception:
        pass
    # 5. Connector capabilities (from configs/capabilities.yaml via detect_all): the integrations, so
    #    "do you support Outlook / Gmail / WhatsApp" is answerable with TRUTHFUL availability instead
    #    of a tool miss. Not chat_native (they are set up in the Cockpit, not invoked as a chat tool),
    #    so they never distort gap_report; explain() opts them in explicitly. Internal capabilities
    #    (core, qa, rtm_portal) are omitted , they are not something a user "supports".
    try:
        from . import capabilities as _caps
        for c in _caps.detect_all():
            key = c.get("capability")
            friendly = _CONNECTOR_FRIENDLY.get(key)
            if not friendly:
                continue
            cards.append({"id": f"connector:{key}", "kind": "connector", "title": friendly,
                          "text": f"{friendly}. {c.get('description', '')} {c.get('enables', '')}",
                          "tier": "connector", "chat_native": False,
                          "ready": bool(c.get("ready")), "configured": bool(c.get("configured"))})
    except Exception:
        pass
    return cards


# User-facing names for the connector capabilities worth answering about in chat.
_CONNECTOR_FRIENDLY = {
    "google": "Gmail, Google Calendar and Tasks",
    "microsoft": "Outlook and Microsoft 365 mail, Calendar and To Do",
    "whatsapp": "WhatsApp messages",
    "voice": "voice input (talk to me instead of typing)",
    "documents": "reading inside PDF and Word documents",
}

# A connector only matches when the query NAMES its product. Generic words ("messages", "email") are
# deliberately excluded: "check my unread messages" is a tool question, not a "do you support X" one.
_CONNECTOR_KEYWORDS = {
    "google": {"gmail", "google"},
    "microsoft": {"outlook", "microsoft", "365", "o365"},
    "whatsapp": {"whatsapp", "waha"},
    "voice": {"voice", "speak", "talk", "talking", "microphone", "mic", "dictate", "dictation"},
    "documents": {"pdf", "pdfs", "docx", "document", "documents"},
}


# Query-side synonym bridge for keyword-only mode (no embedding model installed): the words
# people say mapped to the words the registries use. Small and curated, EN+PL.
_ALIASES = {
    "qr": "pairing phone pair devices lan", "refused": "unreachable lan enable phone bind",
    "wifi": "network lan", "email": "mail gmail imap sync", "inbox": "mail gmail sync",
    "website": "wordpress plugin portal", "lights": "home control state",
    "brain": "model ollama", "poczta": "mail gmail",
    "wtyczka": "plugin wordpress fix", "wtyczke": "plugin wordpress fix",
    "wtyczki": "plugin wordpress fix", "wtyczk": "plugin wordpress fix",
    "naprawic": "fix repair status action", "naprawi": "fix repair status action",
    "strona": "wordpress plugin portal", "stronie": "wordpress plugin portal",
    "telefon": "phone pairing devices", "restart": "stop serve status watchdog",
    # Query-side vocabulary bridges for capability questions ("do you track bugs" -> the bug tools):
    "bug": "bug report log fix status", "bugs": "bug report log fix status",
    "track": "log status monitor report", "voicemail": "voicemail caller message",
    "voicemails": "voicemail caller message", "reminder": "reminder alarm dashboard",
    "reminders": "reminder alarm dashboard", "heating": "home control device",
    "outlook": "microsoft mail calendar", "calendar": "calendar events tasks",
}


def _expand(query: str) -> str:
    extra = " ".join(_ALIASES.get(w, "") for w in _WORD.findall(_fold(query)))
    return query + " " + extra


def search(query: str, top_k: int = 8) -> list[dict[str, Any]]:
    """Hybrid capability search: keyword overlap always, embedding cosine when the optional
    local model is present. Returns cards with a score and the signal that produced it."""
    q = _toks(_expand(query))
    if not q:
        return []
    cards = build_cards()
    scored = []
    # Tie-break toward doing over inspecting: an action-phrased request ("fix", "naprawić",
    # "regenerate") should rank action/status/guide tools above adjacent source-bundle
    # inspection tools that merely share a domain word (external audit A6).
    action_intent = any(w in ("fix", "repair", "action", "status", "regenerate") for w in q)
    for c in cards:
        ct = _toks(c["title"] + " " + c["text"])
        kw = len(q & ct) / len(q) if ct else 0.0
        if action_intent and kw > 0:
            title_l = c["title"].lower()
            if "_source" in title_l or "build_source" in title_l:
                kw *= 0.6
            elif "action" in title_l or "status" in title_l or c["kind"] == "workflow":
                kw = min(1.0, kw * 1.2)
        scored.append((kw, c))
    scored.sort(key=lambda x: -x[0])
    top = scored[: max(top_k * 3, 24)]
    try:
        from .embeddings import embed, cosine
        vecs = embed([query] + [c["title"] + ". " + c["text"][:300] for _, c in top])
        if vecs:
            qv, cvs = vecs[0], vecs[1:]
            rescored = []
            for (kw, c), cv in zip(top, cvs):
                sem = max(0.0, cosine(qv, cv))
                rescored.append((0.5 * kw + 0.5 * sem, "hybrid", c))
            rescored.sort(key=lambda x: -x[0])
            return [{**c, "score": round(s, 3), "signal": sig}
                    for s, sig, c in rescored[:top_k] if s > 0]
    except Exception:
        pass
    return [{**c, "score": round(kw, 3), "signal": "keywords"}
            for kw, c in top[:top_k] if kw > 0]


def gap_report() -> dict[str, Any]:
    """Chat-native vs CLI-only vs cockpit-only. Heuristic: a CLI command or cockpit route
    counts as chat-covered when its tokens substantially overlap some chat tool's card.
    Honest about being a heuristic; the trend matters more than the last digit."""
    cards = build_cards()
    chat = [c for c in cards if c["chat_native"]]
    chat_toks = [(_toks(c["title"] + " " + c["text"]), c["title"]) for c in chat]

    def covered(card: dict[str, Any]) -> bool:
        t = _toks(card["title"] + " " + card["text"])
        if not t:
            return False
        return any(len(t & ct) / len(t) >= 0.5 for ct, _ in chat_toks if ct)

    cli_cards = [c for c in cards if c["kind"] == "cli"]
    cockpit = [c for c in cards if c["kind"] == "route"
               and c["title"].split(" ", 1)[-1].startswith("/cockpit")]
    cli_only = [c["title"] for c in cli_cards if not covered(c)]
    cockpit_only = [c["title"] for c in cockpit if not covered(c)]
    return {"chat_native": len(chat),
            "cli_commands": len(cli_cards), "cli_only": len(cli_only),
            "cockpit_routes": len(cockpit), "cockpit_only": len(cockpit_only),
            "cli_only_examples": sorted(cli_only)[:12],
            "cockpit_only_examples": sorted(cockpit_only)[:12],
            "note": "heuristic token coverage; the trend release-over-release is the signal"}


_DEV_PREFIX = re.compile(r"^\s*(read[- ]only|acting|sensitive)\s*[:.\-]?\s*", re.IGNORECASE)


def _humanise(text: str) -> str:
    """Turn a tool's developer description into a user-facing phrase: drop the tier prefix
    ('Read-only:', 'Acting'), keep the first sentence, and lowercase the leading letter so it reads
    after 'I can ...'. Best-effort from existing metadata, never raises."""
    t = _DEV_PREFIX.sub("", (text or "").strip())
    # First clause only: cut at a sentence end, a colon list, or a parenthetical aside, so the answer
    # stays a clean one-liner instead of trailing the tool's full technical description.
    for sep in (". ", ": ", " ("):
        if sep in t:
            t = t.split(sep)[0]
    t = re.sub(r"\bMY\b", "your", t)  # tool copy is first-person ("read MY messages"); flip the POV
    t = t.strip().rstrip(".:(")
    if len(t) > 96:
        t = t[:93].rstrip() + "..."
    if t and t[0].isupper() and not t.startswith(("I ", "RTM", "WhatsApp", "Home", "Gmail", "Google")):
        t = t[0].lower() + t[1:]
    return t


def _workflow_say(card_id: str) -> tuple[str, str] | None:
    """(title, first example utterance) for a workflow card id, from the guide, or None."""
    try:
        from .capability_guide import WORKFLOWS
        wid = card_id.split(":", 1)[-1]
        for wf in WORKFLOWS:
            if str(wf.get("id")) == wid:
                steps = wf.get("steps") or []
                say = (steps[0].get("say") if steps else "") or ""
                return (str(wf.get("title", "")), say)
    except Exception:
        pass
    return None


def explain(query: str, *, is_guest: bool = False, min_score: float = 0.5) -> str | None:
    """Answer a user's "can you do X / how do I X / do you support X" from the LIVE capability
    registry, so chat recognises ANY registered capability, not just the headline showcase areas.

    Owner-only for now (guests have their own delight path). Deliberately POSITIVE-ONLY: it answers
    only on a confident chat-native match and otherwise returns None to let normal chat handle it,
    so a thin index never makes Mongoose wrongly deny a feature it actually has. Routes and CLI
    commands are suppressed entirely: they are internal surfaces, useless to a chat user ('how do I
    invite a guest' must not answer with 'GET /guest/invite/{token}')."""
    if is_guest:
        return _guest_explain(query)
    # Integrations first: "do you support Outlook / Gmail / WhatsApp" is best answered with truthful
    # availability, from a dedicated match over the small connector set (reliable for short product-
    # name queries, where the global index's long card text dilutes the score).
    conn = _connector_match(query)
    if conn:
        return conn
    try:
        hits = search(query or "", top_k=15)
    except Exception:
        return None
    # Chat-native only for the rest: the surfaces a user reaches by talking. Drop routes and CLI
    # (internal), and the meta capability tools (answering "yes, I can search my own capabilities" to
    # "can you X" is circular and unhelpful).
    _meta = {"tool:capability_search", "tool:capability_guide"}
    native = [h for h in hits if h.get("kind") in ("chat_tool", "workflow")
              and h.get("id") not in _meta]
    native = [h for h in native if h.get("score", 0) >= min_score]
    if not native:
        return None
    best = native[0]
    # Prefer a workflow phrasing when one is close behind the top hit: it is human-authored and can
    # tell the user exactly what to say. Within 0.08 of the best, a workflow wins the headline.
    workflow = next((h for h in native if h.get("kind") == "workflow"
                     and h.get("score", 0) >= best.get("score", 0) - 0.08), None)
    if workflow:
        ws = _workflow_say(workflow.get("id", ""))
        if ws and ws[1]:
            title, say = ws
            primary = f"Yes. To {title[0].lower() + title[1:]}, just say \"{say}\"."
        else:
            primary = f"Yes. I can {_humanise(workflow.get('text', ''))}."
    else:
        primary = f"Yes. I can {_humanise(best.get('text', ''))}."
    # One distinct secondary capability, if there is a genuinely strong, different one.
    secondary = ""
    for h in native[1:]:
        if h.get("id") == best.get("id") or h.get("kind") == "workflow":
            continue
        if h.get("score", 0) >= max(min_score, best.get("score", 0) - 0.12):
            secondary = f" I can also {_humanise(h.get('text', ''))}."
            break
    tail = " (Just ask me here, in your own words.)"
    return primary + secondary + tail


def _connector_match(query: str, *, min_score: float = 0.42) -> str | None:
    """Match a query against the small connector set by friendly NAME only (short text, so a short
    'do you support gmail' query is not diluted the way the full search card is). Returns a truthful
    availability answer, or None below threshold."""
    qtok = _toks(query)
    if not qtok:
        return None
    try:
        from . import capabilities as _caps
        cards = [{"id": f"connector:{c['capability']}", "title": _CONNECTOR_FRIENDLY[c["capability"]],
                  "ready": bool(c.get("ready")), "configured": bool(c.get("configured"))}
                 for c in _caps.detect_all()
                 if c.get("capability") in _CONNECTOR_FRIENDLY
                 and (_CONNECTOR_KEYWORDS.get(c.get("capability"), set()) & qtok)]
    except Exception:
        return None
    if not cards:
        return None
    best, score = _best_row(query, cards, lambda c: c["title"])
    if not best or score < min_score:
        return None
    return _connector_answer(best)


def _connector_answer(card: dict[str, Any]) -> str:
    """A connector question answered with TRUTHFUL availability: supported-and-connected,
    supported-but-not-set-up, or supported-once-its-dependency-is-installed."""
    name = card.get("title", "that")
    ready = card.get("ready")
    configured = card.get("configured")
    if ready and configured:
        n = (name[0].upper() + name[1:]) if name else name
        return f"Yes. {n} is connected and working, so just ask me here."
    if ready and not configured:
        return (f"Yes, I support {name}. It is not connected yet: set it up in the Cockpit's Setup "
                f"tab, then just ask.")
    return (f"Yes, {name} is supported but needs finishing first (a dependency is missing). "
            f"Open the Setup tab to complete it.")


def _best_row(query: str, rows: list[dict[str, Any]], textfn) -> tuple[dict[str, Any] | None, float]:
    """Best-matching row for a query: embedding cosine when the model is present, else keyword
    overlap. Returns (row, score) or (None, 0.0)."""
    texts = [textfn(r) for r in rows]
    try:
        from .embeddings import embed, cosine
        vecs = embed([query or ""] + texts)
        if vecs:
            qv = vecs[0]
            scored = sorted(((cosine(qv, vecs[i + 1]), rows[i]) for i in range(len(rows))),
                            key=lambda x: x[0], reverse=True)
            return (scored[0][1], scored[0][0]) if scored else (None, 0.0)
    except Exception:
        pass
    q = _toks(query)
    best, bs = None, 0.0
    for r, t in zip(rows, texts):
        tt = _toks(t)
        if tt:
            s = len(q & tt) / max(1, len(q))
            if s > bs:
                best, bs = r, s
    return best, bs


def _guest_explain(query: str, *, min_score: float = 0.48) -> str | None:
    """Answer a GUEST's "can you X" strictly from the guest-safe capability set (guest_surpriser),
    NEVER the owner tool registry, so it can never leak that Mongoose can read the owner's WhatsApp
    or RTM ledger. Positive-only, same as the owner path."""
    try:
        from .guest_surpriser import guest_can_do
        rows = [r for r in (guest_can_do().get("can_do") or []) if isinstance(r, dict)]
    except Exception:
        return None
    if not rows:
        return None
    best, score = _best_row(query, rows, lambda r: f"{r.get('label', '')}. {r.get('blurb', '')}")
    if not best or score < min_score:
        return None
    blurb = (best.get("blurb") or "").strip().rstrip(".")
    return f"Yes. {blurb}. Just say it here; it stays in your own space."
