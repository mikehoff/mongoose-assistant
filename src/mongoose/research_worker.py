from __future__ import annotations

import json
from typing import Any

# Silent research worker.
#
# Architecture principle (all three design notes converge here): in one conversation,
# only the CONVERSATION OWNER speaks to the user. Specialist models are silent
# workers that return STRUCTURED EVIDENCE, which the owner then phrases. This keeps a
# single coherent voice in a thread: no tone drift, no two models reinterpreting the
# same reference differently.
#
# This module is the worker side. research_worker() runs the configured research model
# (e.g. qwen3.5:9b) and returns notes as JSON: findings + sources, NOT a finished
# answer. The owner model is what turns these notes into the reply the user reads.
#
# The worker is deliberately incapable of being the speaker: it is prompted to emit
# notes only, and its output is parsed into a structure. If you want its text shown to
# the user, the owner has to phrase it, that's the guarantee.

_WORKER_SYSTEM = (
    "You are a silent research worker for an assistant called Mongoose. You do NOT "
    "talk to the user. You gather and structure evidence for another model (the "
    "conversation owner) to phrase. You are given RETRIEVED SOURCES (real local "
    "search results). Base your findings on those sources and cite them by their "
    "[n] index. Do NOT invent sources or facts not supported by the retrieved "
    "material; if the sources don't answer the question, say so in 'gaps'. "
    "Return ONLY a JSON object: "
    '{"findings": ["<note> [n]", ...], "used_sources": [<int index>, ...], '
    '"confidence": "low|medium|high", "gaps": ["<what the sources did not cover>", ...]}. '
    "No prose, no greeting, no markdown fences."
)


def _retrieve_local(question: str, limit: int = 8) -> list[dict[str, Any]]:
    """Read-only retrieval from local sources (files, attachments, email) via the
    unified find. Returns real source records (title, where, snippet, open handle),
    so citations are grounded in actual local data, not model assertion."""
    try:
        from .services.find import find
        res = find(question, limit=limit)
        return res.get("results", []) or []
    except Exception:
        return []


# Below this many local hits, the question is treated as "thin" locally and we
# augment with web search (only if SearXNG is configured). Web is a FALLBACK, not a
# default: a well-answered local query never touches the network.
_THIN_LOCAL_THRESHOLD = 2


async def _retrieve_web(question: str, limit: int = 5) -> tuple[list[dict[str, Any]], str | None]:
    """Web retrieval via SearXNG, only when enabled. Returns (results, note). note is
    a human reason when web was skipped or failed, so the worker can record it as a
    gap rather than silently omitting."""
    from . import web_search
    if not web_search.is_enabled():
        return [], "web search disabled (no SearXNG configured)"
    try:
        results = await web_search.search_web(question, limit=limit)
        return results, None
    except web_search.SearxngError as exc:
        return [], f"web search unavailable: {exc}"


def _sources_block(sources: list[dict[str, Any]]) -> str:
    if not sources:
        return "(no local sources retrieved)"
    lines = []
    for i, s in enumerate(sources, 1):
        title = s.get("title") or s.get("where") or "untitled"
        snippet = (s.get("snippet") or "").replace("\n", " ")[:240]
        lines.append(f"[{i}] {title} ({s.get('kind', '?')}): {snippet}")
    return "\n".join(lines)


def _coerce_notes(raw: str) -> dict[str, Any]:
    """Parse the worker's JSON notes; on failure, wrap the raw text as a single
    low-confidence finding so the owner still has something to phrase (and we never
    leak the worker's raw text to the user as if it were the answer)."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "findings" in obj:
            obj.setdefault("used_sources", [])
            obj.setdefault("confidence", "low")
            obj.setdefault("gaps", [])
            return obj
    except Exception:
        pass
    return {"findings": [text[:500]] if text else [],
            "used_sources": [], "confidence": "low",
            "gaps": ["worker did not return structured notes; treat as unverified"]}


async def research_worker(question: str, *, context: str | None = None,
                          model: str | None = None,
                          retrieve: bool = True, allow_web: bool = True) -> dict[str, Any]:
    """Run the research worker over RETRIEVED sources and return STRUCTURED NOTES
    (not a user answer) with grounded source records.

    Retrieval is LOCAL FIRST (read-only find: files/attachments/email). If local is
    thin (< _THIN_LOCAL_THRESHOLD hits) AND SearXNG is configured AND allow_web, it
    augments with web snippets (a fallback, not a default: a well-answered local
    query never touches the network). The model cites retrieved sources by index and
    is told not to invent sources; the return carries the real records, so citations
    are grounded. retrieve=False makes it a pure analysis pass (honestly labelled).
    """
    from .llm import ask_ollama
    from .settings import get_settings
    settings = get_settings()
    worker_model = model or settings.ollama_research_model

    sources: list[dict[str, Any]] = []
    web_note: str | None = None
    used_web = False
    if retrieve:
        sources = _retrieve_local(question)
        # local-first: only reach for web when local is thin
        if allow_web and len(sources) < _THIN_LOCAL_THRESHOLD:
            web_results, web_note = await _retrieve_web(question)
            if web_results:
                used_web = True
                sources = sources + web_results

    parts = [question]
    if context:
        parts.append(f"Context:\n{context}")
    parts.append("Retrieved sources:\n" + _sources_block(sources))
    prompt = "\n\n".join(parts)

    raw = await ask_ollama(prompt, system=_WORKER_SYSTEM, role="research", model=worker_model)
    notes = _coerce_notes(raw)
    # Attach the REAL source records the findings should be grounded in, by index.
    notes["sources"] = [
        {"index": i + 1, "title": s.get("title") or s.get("where"),
         "kind": s.get("kind"), "where": s.get("where"),
         "open": s.get("open"), "snippet": (s.get("snippet") or "")[:240]}
        for i, s in enumerate(sources)
    ]
    notes["grounded"] = bool(sources)
    notes["used_web"] = used_web
    # If web was skipped/failed when local was thin, record it as a gap (transparency).
    if retrieve and not used_web and web_note and len(notes["sources"]) < _THIN_LOCAL_THRESHOLD:
        notes.setdefault("gaps", []).append(web_note)
    notes["worker_model"] = worker_model
    return notes


def format_notes_for_owner(notes: dict[str, Any]) -> str:
    """Render worker notes as an evidence block to hand to the conversation owner as
    context. This is NOT shown to the user: the owner reads it and writes the reply."""
    lines = ["[research notes, phrase these in your own voice; do not quote verbatim]"]
    for f in notes.get("findings", []):
        lines.append(f"- {f}")
    sources = notes.get("sources", [])
    if sources:
        lines.append("grounded sources:")
        for s in sources:
            if isinstance(s, dict):
                label = s.get("title") or s.get("where") or "source"
                lines.append(f"  [{s.get('index')}] {label} ({s.get('kind', '?')})")
            else:
                lines.append(f"  - {s}")
    elif not notes.get("grounded", False):
        lines.append("(no local sources retrieved; this is model analysis, not "
                     "grounded research, weight accordingly)")
    if notes.get("gaps"):
        lines.append("unresolved: " + "; ".join(str(g) for g in notes["gaps"]))
    lines.append(f"confidence: {notes.get('confidence', 'low')}")
    return "\n".join(lines)


def owner_is_speaker_only(settings) -> bool:
    """The enforced invariant: workers never speak. Always True under owner_only
    policy (the only supported policy). Exposed so callers/tests can assert it."""
    return getattr(settings, "speaker_policy", "owner_only") == "owner_only"
