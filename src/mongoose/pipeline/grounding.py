"""The fabrication firewall.

enforce(draft, evidence) is the single choke every turn passes through (called by the sealed
TurnHandler.run). It cannot be bypassed by a trap door because the trap door never calls the model directly
and never renders the string: it only returns a Draft, which lands here.

Three tiers, so rigour about facts buys freedom for perception:
  - facts       must cite a source that EXISTS, and every number in the claim must appear in that source's
                data (so "7/7" citing a source that says "3/7" is caught, not just an invented source_id).
  - inferences  are kept only if every from_id is real evidence. They are reads, not results.
  - unknowns    pass through untouched: naming what is missing is honest, not a failure.
  - voice       (tone/framing) is kept only if it smuggles no number; figures belong in facts, verified.

Anything that fails is moved to `rejected` and never reaches the user.
"""
from __future__ import annotations

import re

from .base import Draft, Evidence, Fact, Inference

# A "significant" number: a run of digits possibly joined by . / : - (e.g. 3/7, 2.8, 01-02-03, 580.52, 4).
_NUM = re.compile(r"\d+(?:[./:\-]\d+)*")


def _numbers(text: str) -> list[str]:
    return _NUM.findall(text or "")


def _blob(data: object) -> str:
    """Flatten a source row to a string so a claim's numbers can be checked as substrings of the truth."""
    return str(data)


def _atoms(value: object) -> list[str]:
    """The quotable leaf strings of a value: a scalar is itself; a list/dict yields its elements/values.
    So a fact can cite one item of a tool list or a capability list, not just a scalar field."""
    if isinstance(value, dict):
        out: list[str] = []
        for v in value.values():
            out += _atoms(v)
        return out
    if isinstance(value, (list, tuple, set)):
        out = []
        for v in value:
            out += _atoms(v)
        return out
    return [str(value).strip()]


def _fields_quoted(data: object, text: str) -> list[str]:
    """The fields of a source one of whose real leaf-values appears verbatim in the claim text. This is what
    a fact must have at least one of: it forces the claim to quote a real value from the real source. We only
    count values of length >= 2 so a stray single digit (e.g. timeouts=0) cannot be a spurious 'match', which
    is what keeps a value-less narrative ("scored higher on logic") from passing."""
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    text = text or ""
    for key, value in data.items():
        if any(len(atom) >= 2 and atom in text for atom in _atoms(value)):
            out.append(key)
    return out


def enforce(draft: Draft, evidence: Evidence) -> tuple[Draft, list[str]]:
    rejected: list[str] = []
    ids = evidence.ids()

    kept_facts: list[Fact] = []
    for f in draft.facts:
        src = evidence.get(f.source_id)
        if src is None:
            rejected.append(f"fact cites no real source ({f.source_id!r}): {f.text}")
            continue
        # The claim MUST quote a real value of at least one field of the cited source. This is the whole
        # anti-fabrication property, and deriving the field from the value (rather than trusting the model
        # to name it) makes it robust to a loose model while staying strict: "scored higher on logic" quotes
        # no real value, so it dies here even though its source_id is real.
        quoted = _fields_quoted(src.data, f.text)
        if not quoted:
            rejected.append(f"fact quotes no real value of source {f.source_id!r}: {f.text}")
            continue
        # And no OTHER number in the claim may be invented (a real value plus a smuggled extra figure).
        blob = _blob(src.data)
        missing = [n for n in _numbers(f.text) if n not in blob]
        if missing:
            rejected.append(f"fact numbers {missing} absent from source {f.source_id!r}: {f.text}")
            continue
        # record the field(s) we actually verified against, for the trace / 'show me the source'
        if not f.field or f.field not in quoted:
            f.field = quoted[0]
        kept_facts.append(f)

    kept_inferences: list[Inference] = []
    for inf in draft.inferences:
        if not inf.from_ids or any(fid not in ids for fid in inf.from_ids):
            rejected.append(f"inference not drawn from real evidence: {inf.text}")
            continue
        kept_inferences.append(inf)

    # In a FACTUAL answer, voice may frame and warm but must not carry a figure (an unverified fact in
    # disguise). In pure conversation (draft.factual is False), a number is just a number (a time, a count),
    # so we leave it alone; the safety there is that factual questions do not route to a conversation handler.
    voice = draft.voice or ""
    if draft.factual and _numbers(voice):
        rejected.append(f"voice framing carried an unverified figure and was dropped: {voice}")
        voice = ""

    clean = Draft(
        facts=kept_facts,
        inferences=kept_inferences,
        unknowns=list(draft.unknowns),
        voice=voice,
        factual=draft.factual,
    )
    return clean, rejected
