from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .file_index import search_files
from .email_index import search_email
from .whatsapp import search_messages as search_whatsapp
from .memory import search_facts
from .calendar_tasks import events_today, tasks_today


def _terms(text: str) -> set[str]:
    return {w for w in ''.join(c.lower() if c.isalnum() else ' ' for c in (text or '')).split()
            if len(w) > 2}


def _safe(text: Any, n: int = 180) -> str:
    s = str(text or '').strip()
    return (s[: n - 1] + '…') if len(s) > n else s


def _match_score(query_terms: set[str], *parts: Any) -> int:
    terms = set()
    for p in parts:
        terms |= _terms(str(p or ''))
    return len(query_terms & terms)


def _event_evidence(query_terms: set[str]) -> list[dict[str, Any]]:
    out = []
    try:
        for e in events_today():
            score = _match_score(query_terms, e.get('title'), e.get('calendar_name'))
            if score or not query_terms:
                out.append({
                    'source': 'calendar', 'title': _safe(e.get('title') or 'Calendar event'),
                    'when': e.get('start_at'), 'score': score, 'detail': _safe(e.get('calendar_name') or '')})
    except Exception:
        pass
    return out


def _task_evidence(query_terms: set[str]) -> list[dict[str, Any]]:
    out = []
    try:
        for t in tasks_today():
            score = _match_score(query_terms, t.get('title'), t.get('list_name'))
            if score or not query_terms:
                out.append({'source': 'task', 'title': _safe(t.get('title') or 'Task'),
                            'when': t.get('due_at'), 'score': score})
    except Exception:
        pass
    return out


def _search_evidence(query: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for em in search_email(query, limit=5):
            priv = str(em.get('privacy', '')).lower()
            title = '[private email]' if priv in ('high', 'secret') else _safe(em.get('subject') or 'Email')
            out.append({'source': 'email', 'title': title, 'when': em.get('received_at') or em.get('sent_at'),
                        'score': 1, 'detail': _safe(em.get('sender') or '')})
    except Exception:
        pass
    try:
        for f in search_files(query, limit=5):
            out.append({'source': 'file', 'title': _safe(f.get('title') or f.get('path') or 'File'),
                        'when': f.get('indexed_at'), 'score': 1, 'detail': _safe(f.get('path') or '')})
    except Exception:
        pass
    try:
        for w in search_whatsapp(query, limit=5):
            out.append({'source': 'whatsapp', 'title': _safe(w.get('contact') or 'WhatsApp'),
                        'when': w.get('received_at') or w.get('sent_at'), 'score': 1,
                        'detail': _safe(w.get('body') or '[private message]')})
    except Exception:
        pass
    try:
        for m in search_facts(query, limit=5):
            if isinstance(m, dict):
                title = m.get('fact') or ''
                when = m.get('updated_at') or m.get('created_at')
            else:
                title, when = str(m), None
            out.append({'source': 'memory', 'title': _safe(title), 'when': when, 'score': 1})
    except Exception:
        pass
    return out


async def prepare_card(topic: str) -> dict[str, Any]:
    """Prepare-me card: a deterministic, evidence-grounded context pack for an event,
    person, project, task, place, or upcoming situation. It never invents context; it
    gathers what Mongoose can see and names the limits."""
    topic = _safe(topic or 'today', 120)
    q_terms = _terms(topic)
    evidence: list[dict[str, Any]] = []
    evidence.extend(_event_evidence(q_terms))
    evidence.extend(_task_evidence(q_terms))
    evidence.extend(_search_evidence(topic))
    # rank exact-ish evidence first, but keep source diversity
    evidence.sort(key=lambda e: (e.get('score', 0), e.get('when') or ''), reverse=True)
    seen = set()
    deduped = []
    for e in evidence:
        key = (e.get('source'), e.get('title'), e.get('when'))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    evidence = deduped[:12]

    sources = sorted({e['source'] for e in evidence if e.get('source')})
    open_loops = []
    if any(e.get('source') == 'task' for e in evidence):
        open_loops.append('There is at least one task connected to this context.')
    if any(e.get('source') == 'email' for e in evidence):
        open_loops.append('There is recent or indexed email evidence to review.')
    if any(e.get('source') == 'whatsapp' for e in evidence):
        open_loops.append('There is WhatsApp context connected to this.')

    # useful context is the top evidence rendered as human lines, not model prose.
    useful_context = [f"{e['source']}: {e.get('title', '')}" for e in evidence[:6]]
    known_limits = []
    if 'email' not in sources:
        known_limits.append('No matching email evidence found in the local index.')
    if 'file' not in sources:
        known_limits.append('No matching file evidence found in the local index.')
    if 'calendar' not in sources and q_terms:
        known_limits.append('No matching calendar event found today.')
    if not evidence:
        known_limits.append('Mongoose found no local evidence yet; this card is a starting point, not a conclusion.')

    suggested = 'Review the strongest evidence first.' if evidence else 'Add or sync relevant calendar, mail, files or notes, then ask again.'
    if any(e.get('source') == 'calendar' for e in evidence):
        suggested = 'Open the calendar context, then check recent email/files before the event.'
    if any(e.get('source') == 'whatsapp' for e in evidence):
        suggested = 'Check the WhatsApp context before responding or meeting.'

    return {
        'topic': topic,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'why_now': f"You asked Mongoose to prepare you for: {topic}.",
        'useful_context': useful_context,
        'open_loops': open_loops,
        'what_to_check': useful_context[:4],
        'risks_or_blockers': [],
        'suggested_next_step': suggested,
        'evidence': evidence,
        'sources_used': sources,
        'known_limits': known_limits,
        'confidence': 'grounded' if evidence else 'thin',
    }


def format_prepare(card: dict[str, Any]) -> str:
    lines = [f"Prepare me for: {card.get('topic')}", card.get('why_now', '')]
    if card.get('useful_context'):
        lines.append('\nUseful context:')
        lines.extend(f"  • {x}" for x in card['useful_context'])
    if card.get('open_loops'):
        lines.append('\nOpen loops:')
        lines.extend(f"  • {x}" for x in card['open_loops'])
    lines.append(f"\nNext step: {card.get('suggested_next_step')}")
    if card.get('known_limits'):
        lines.append('\nKnown limits:')
        lines.extend(f"  • {x}" for x in card['known_limits'])
    return '\n'.join(x for x in lines if x)
