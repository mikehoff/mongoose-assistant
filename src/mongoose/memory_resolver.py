"""Memory target resolver, the general solution to referent resolution: when the user names a
remembered thing in different words than it was stored in, recognise it's the same memory WITHOUT
deleting the wrong thing.

The mistake this avoids is two-sided:
  - wording-identity over-reliance: "forget Hawaiian" should be able to reach "likes pineapple on
    pizza" even though they share no token; and
  - loose-association overreach: "forget pineapple" must NOT silently delete "likes Hawaiian pizza"
    when several pineapple-ish memories exist, it should ask.

So this is meaning-aware candidate resolution + a conservative deletion policy. It is NOT a vector
database and NOT a model call; it's deterministic. A small canonical-concept table provides ONE
evidence channel (synonyms/aliases/acronyms), combined with exact/lexical/token signals into a score
with confidence tiers. A concept group is general infrastructure seeded with a few real groups (incl.
the Hawaiian/pineapple-pizza case and the user's own domain terms); adding more is data, not redesign.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# --- canonical concept groups: ONE evidence channel, not the whole design --------------------------
# Each group maps a concept key to the surface phrases that denote the same underlying thing. Used to
# tell that two differently-worded references point at the same memory. Seeded with the pizza case
# plus the user's real domain aliases (work, HA/Home Assistant, Mongoose/the assistant, etc.).
# Phrases are matched as whole-word substrings, longest first, so "home assistant" wins over "home".
CONCEPT_GROUPS: dict[str, list[str]] = {
    "pizza_hawaiian_pineapple": [
        "hawaiian pizza", "hawaiian", "pineapple pizza", "pineapple on pizza",
        "pizza with pineapple", "pineapple topping",
    ],
    "home_assistant": ["home assistant", "homeassistant", "hass", "ha"],
    "employer_work": ["examplecorp", "the apprenticeship", "l4", "the office", "my job"],
    "mongoose_assistant": ["mongoose", "the mac mini assistant", "the local assistant", "the household ai"],
    "rtm_company": ["example court rtm", "example court", "the rtm company", "the rtm", "rtm company"],
    "gym_plan": ["the gym plan", "my split", "my current split", "the programme"],
    "dog": ["the dog", "my dog"],
    "legal_matter": ["the legal matter", "the complaint", "the formal complaint"],
}



def concept_groups() -> dict[str, list[str]]:
    """The concept table in force. A site profile can supply its own complete table (lexicon
    memory.concept_groups, same shape as CONCEPT_GROUPS) holding the household's real aliases; without
    one, the generic table above is used with its RTM group named after the profile's RTM company."""
    from . import profile as _profile
    own = _profile.lexicon("memory.concept_groups", None)
    if own is not None:
        return own
    rtm = _profile.rtm_name().lower()
    groups = dict(CONCEPT_GROUPS)
    groups["rtm_company"] = [f"{rtm} rtm", rtm] + [p for p in CONCEPT_GROUPS["rtm_company"]
                                                   if p not in ("example court rtm", "example court")]
    return groups


# reverse index: phrase -> concept key (phrases can belong to >1 group in principle), rebuilt only when
# the active concept table changes
_INDEX_CACHE: tuple[Any, list[tuple[str, str]]] | None = None


def _phrase_to_concepts() -> list[tuple[str, str]]:
    global _INDEX_CACHE
    groups = concept_groups()
    if _INDEX_CACHE is None or _INDEX_CACHE[0] != groups:
        index = sorted(
            ((p.lower(), key) for key, phrases in groups.items() for p in phrases),
            key=lambda t: -len(t[0]),  # longest phrase first
        )
        _INDEX_CACHE = (groups, index)
    return _INDEX_CACHE[1]

_STOPWORDS = {"the", "a", "an", "user", "i", "is", "are", "was", "of", "on", "in", "to", "and",
              "my", "that", "it", "likes", "dislikes", "like", "dislike", "fond", "really",
              "generally", "stuff", "thing", "things"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _STOPWORDS}


def _fact_polarity(fact: str) -> str:
    low = _norm(fact)
    if low.startswith("the user likes "):
        return "likes"
    if low.startswith("the user dislikes "):
        return "dislikes"
    return ""


def concepts_for(text: str, _index: list[tuple[str, str]] | None = None) -> set[str]:
    """Concept keys a phrase denotes (via the alias table). Whole-word/phrase matches only. Loops pass
    `_index` (from _phrase_to_concepts()) so the profile is read once per scan."""
    t = _norm(text)
    found = set()
    for phrase, key in (_index if _index is not None else _phrase_to_concepts()):
        # whole-word boundary match so 'ha' doesn't fire inside 'Hawaiian'
        if re.search(r"\b" + re.escape(phrase) + r"\b", t):
            found.add(key)
    return found


@dataclass
class Candidate:
    id: int
    fact: str
    score: float
    tier: str          # strong | medium | weak
    why: str           # which channel(s) fired, for transparency/receipts


def score_candidates(query: str, facts: list[dict], polarity_hint: str = "") -> list[Candidate]:
    """Score each stored fact against the forget/update target across evidence channels. facts is a
    list of {"id":int, "fact":str}. Returns candidates sorted best-first.

    Channels (strongest to weakest):
      exact normalised fact/object match     -> strong
      shared canonical concept (alias/acronym/synonym) -> strong
      object substring (either direction)    -> medium
      distinctive token overlap              -> medium/weak by proportion
    A concept match is what lets 'Hawaiian' reach 'pineapple on pizza'. Generic token overlap alone
    (e.g. only 'pizza' shared) stays weak, so it can't delete on its own.
    """
    qn = _norm(query)
    qtok = _tokens(query)
    index = _phrase_to_concepts()
    qconcepts = concepts_for(query, index)
    out: list[Candidate] = []
    for f in facts:
        fact = f["fact"]
        fp = _fact_polarity(fact)
        if polarity_hint and fp and fp != polarity_hint:
            continue
        fn = _norm(fact)
        ftok = _tokens(fact)
        fconcepts = concepts_for(fact, index)
        score = 0.0
        why = []

        # exact / near-exact: the query phrase IS essentially the stored object
        if qn and (qn == fn or qn in fn and len(qtok) >= 2 and qtok <= ftok):
            score += 3.0
            why.append("exact")

        # shared canonical concept = same underlying referent (the cross-synonym channel)
        shared = qconcepts & fconcepts
        if shared:
            score += 2.5
            why.append("concept:" + ",".join(sorted(shared)))

        # substring either direction (distinctive, not just a stopword)
        if qn and qn in fn and "exact" not in why:
            score += 1.2
            why.append("substring")

        # distinctive token overlap, normalised by the query's own content tokens
        if qtok:
            overlap = len(qtok & ftok)
            if overlap:
                frac = overlap / len(qtok)
                score += 1.0 * frac           # proportional, so a single shared token among many is weak
                why.append(f"tokens:{overlap}/{len(qtok)}")

        if score <= 0:
            continue
        tier = "strong" if score >= 2.5 else ("medium" if score >= 1.2 else "weak")
        out.append(Candidate(id=f["id"], fact=fact, score=round(score, 3), tier=tier, why=" + ".join(why)))

    out.sort(key=lambda c: c.score, reverse=True)
    return out


def resolve_forget(query: str, facts: list[dict], polarity_hint: str = "") -> dict:
    """Apply the conservative decision policy to scored candidates. Returns:
      {"action":"delete", "candidate":{id,fact}}  : one strong, clear match
      {"action":"confirm", "candidates":[{id,fact},...]}  : several plausible / no clear winner
      {"action":"none"}                                    : nothing matched
    Deletion is only chosen for a STRONG match that clearly beats the runner-up. Anything else asks
    or declines, the safe failure mode is ambiguity, never accidental deletion."""
    cands = score_candidates(query, facts, polarity_hint=polarity_hint)
    if not cands:
        return {"action": "none"}
    top = cands[0]
    strong = [c for c in cands if c.tier == "strong"]
    # one strong match, or a strong winner that clearly beats the next candidate -> delete
    if top.tier == "strong" and (len(strong) == 1 and (len(cands) == 1 or top.score >= cands[1].score + 1.0)):
        return {"action": "delete", "candidate": {"id": top.id, "fact": top.fact}, "why": top.why}
    # multiple strong, or strong-but-close, or only medium/weak -> ask. Surface medium+ candidates by
    # preference; if there are only weak matches but more than one, still ASK (the user named
    # something, just vaguely) rather than silently doing nothing, asking is safe, blind deletion is
    # not. A single weak match alone is too thin to act on or to confidently single out, so decline.
    plausible = [c for c in cands if c.tier in ("strong", "medium")]
    if plausible:
        return {"action": "confirm", "candidates": [{"id": c.id, "fact": c.fact} for c in plausible]}
    weak = [c for c in cands if c.tier == "weak"]
    if len(weak) >= 2:
        return {"action": "confirm", "candidates": [{"id": c.id, "fact": c.fact} for c in weak]}
    return {"action": "none"}
