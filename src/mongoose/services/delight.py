from __future__ import annotations

import hashlib
import random
from datetime import datetime, timezone
from typing import Any

# Delight, the warm, interest-aware side of Mongoose.
#
# Mongoose is not just the household/RTM operator. It's a companion: it can lighten the day with a
# cute joke or a fun fact that actually connects to what the owner cares about, for example gardening and old
# Macs, history, philosophy of mind, cooking, pets, writing. Deliberately NOT
# his working domain (leasehold/RTM): reciting expertise back to the expert is noise, not delight.
#
# Three honesty rules, because a companion that fakes things isn't warm, it's hollow:
#   1. The SHIPPED nuggets below are real, hand-checked, and tied to Alex's interests, not filler.
#   2. When a local model generates a fresh one, it's labelled model-sourced (could be wrong), so a
#      "fun fact" is never passed off as verified when it isn't.
#   3. It never repeats the same nugget within a window (same de-dupe discipline as the surpriser).
#
# This is deliberately separate from the surpriser (cross-domain correlations) and from the brain's
# task routes: delight is its own small thing, on its own merits.

_RECENT_WINDOW = 1000   # don't repeat a nugget within the last N served (the cache 'used' flag is the
                        # primary, permanent dedup; this window guards the model/curated fallback paths
                        # AND is shared across private + public chat, so a fact seen in one is not reused
                        # in the other. 40 was far too small, repetition was noticeable within a session.)


# Example interests, with a few genuinely-good shipped nuggets each. Kept small and real; the model
# tier (when available) extends these rather than replacing them.
_INTERESTS: dict[str, dict[str, list[str]]] = {
    "gardening": {
        "facts": [
            "A/UX, Apple's old Unix, let you run Macintosh apps and Unix side by side in the late "
            "'80s, a GUI-over-Unix idea the mainstream took another decade to settle on.",
            "The classic Mac's 400K floppy used variable-speed spinning (CLV-ish): the drive slowed "
            "for outer tracks so it could pack more sectors there. Beautiful, and a nightmare to "
            "clone on commodity PC drives.",
            "The original 1984 Macintosh shipped with 128K of RAM and no fan, Jobs insisted on "
            "silence, which is partly why early Macs ran so hot.",
        ],
        "jokes": [
            "I tried to explain QEMU to the dog. He just wanted to emulate a nap.",
            "Why did the old Mac refuse therapy? It had too many unresolved system extensions.",
        ],
    },
    "local_history": {
        "facts": [
            "The Domesday Book of 1086 surveyed England so thoroughly that historians still use it "
            "to trace individual villages today.",
            "Many old English market towns still hold their markets on the same weekday granted "
            "by a medieval royal charter.",
        ],
        "jokes": [
            "History is just gossip that survived long enough to get footnotes.",
        ],
    },
    "philosophy_of_mind": {
        "facts": [
            "Mary's Room (Frank Jackson, 1982) asks whether a scientist who knows every physical "
            "fact about colour, but has only seen black and white, learns something new the first "
            "time she sees red. People still don't agree.",
            "The word 'qualia' barely existed before the 20th century; the hard problem is old, but "
            "the vocabulary for arguing about it is surprisingly recent.",
        ],
        "jokes": [
            "I'd tell you a joke about philosophical zombies, but you'd behave exactly as if you "
            "found it funny either way.",
        ],
    },
    "cooking": {
        "facts": [
            "Salting pasta water to roughly 1%, about a tablespoon per litre, seasons the pasta "
            "from the inside in a way you can't add back later at the plate.",
            "Resting steak isn't superstition: the muscle fibres relax and reabsorb juice, so a "
            "few minutes off the heat genuinely keeps more of it on the board, not the plate.",
        ],
        "jokes": [
            "I wanted to make a joke about reduction sauces, but I kept boiling it down and now "
            "there's barely anything left.",
        ],
    },
    "dog": {
        "facts": [
            "Dogs don't just smell more than us, they smell in stereo, each nostril samples "
            "slightly separately, so a dog gets a directional map of a scent, not just its presence.",
        ],
        "jokes": [
            "The dog has mastered one command flawlessly: the one where I sit and he gets the treat.",
            "I asked the dog to review the build plan. He approved it by lying on the keyboard.",
        ],
    },
    "poetry": {
        "facts": [
            "The colour teal sits right on the cyan-green edge, which is partly why it reads as "
            "'a blue that became green' depending on what's next to it. Context literally changes "
            "the hue you perceive.",
        ],
        "jokes": [
            "Your narrator drifts, the prose drifts, the reader drifts. The only thing anchored is "
            "the crystal fruit bowl, and even that's just catching the light.",
        ],
    },
    # NOTE: leasehold/RTM is deliberately NOT a delight interest. It is the owner's expert working
    # domain (the owner works in it), so reciting Right-to-Manage or service-charge trivia back
    # to him is noise, not delight, and it kept surfacing as a "suggestion" precisely because he
    # talks about RTM constantly. Delight is for his hobbies (gardening, history,
    # philosophy, cooking, pets, writing), never his day job. See surpriser PROACTIVE noise.
}

# Memory words that nudge an interest up the order in _live_interests (interest key -> cue words).
_INTEREST_HINTS: dict[str, tuple[str, ...]] = {
    "gardening": ("retro", "a/ux", "qemu", "mac", "amiga", "vintage comput"),
    "local_history": ("history", "museum", "castle", "medieval", "archive"),
    "philosophy_of_mind": ("philosophy", "consciousness", "qualia", "mind"),
    "cooking": ("cook", "recipe", "kitchen", "sauce", "pasta"),
    "dog": ("dog", "puppy", "walk"),
    "poetry": ("poetry", "fiction", "story", "writing"),
}


def _interests() -> dict[str, dict[str, list[str]]]:
    """The interest library in force: the example set above, or the site profile's own owner interests
    (lexicon delight.interests, same shape as _INTERESTS)."""
    from .. import profile as _profile
    return _profile.lexicon("delight.interests", _INTERESTS)


def _interest_hints() -> dict[str, tuple[str, ...]]:
    """Interest cue words: the example set above, or the profile's own (lexicon delight.interest_hints)."""
    from .. import profile as _profile
    own = _profile.lexicon("delight.interest_hints", None)
    if own is None:
        return _INTEREST_HINTS
    return {k: tuple(v) for k, v in own.items()}


_GENERIC_FACTS = [
    # Science / nature
    "Honey doesn't really spoil, sealed pots found in ancient tombs were still edible thousands "
    "of years later, thanks to low water and natural acidity.",
    "Octopuses have three hearts, and two of them stop beating when the animal swims, which is part "
    "of why they prefer crawling.",
    "Bananas are slightly radioactive, they're rich in potassium, and a small fraction of natural "
    "potassium is the isotope potassium-40.",
    "A teaspoon of neutron-star material would weigh about as much as a mountain, because the matter "
    "is packed to nuclear density.",
    "Water can boil and freeze at the same time at a specific low pressure called the triple point.",
    "Sharks predate trees: sharks have been around roughly 400 million years, and the earliest trees "
    "appeared a bit later.",
    "Hot water can freeze faster than cold water under some conditions, an odd effect named after a "
    "schoolboy, Erasto Mpemba, who noticed it making ice cream.",
    "Your stomach gets a fresh inner lining every few days, because the acid that digests food would "
    "otherwise digest the stomach itself.",
    # Psychology / mind
    "The 'mere exposure effect' means we tend to like things simply because we've seen them before, "
    "even with no other reason.",
    "Hearing your own name in a noisy room cuts through because of the 'cocktail party effect', "
    "attention is tuned to self-relevant signals.",
    "People reliably misremember details after the fact; memory is reconstructive, not a recording, "
    "which is why confident memories can still be wrong.",
    "The 'Zeigarnik effect' is the tendency to remember unfinished tasks better than finished ones, "
    "open loops nag at attention.",
    "Spending on experiences tends to make people happier than spending on things, partly because we "
    "adapt to possessions but keep re-enjoying memories.",
    "We judge a whole experience largely by its peak and its end, not its average, the "
    "'peak-end rule'.",
    # Universe / space
    "There are more stars in the observable universe than grains of sand on every beach on Earth.",
    "A day on Venus is longer than its year: it rotates so slowly that one spin takes longer than one "
    "orbit of the Sun.",
    "Saturn's density is low enough that, given a big enough bathtub, the planet would float on water.",
    "Light from the Sun takes about eight minutes to reach Earth, so you always see the Sun as it was "
    "eight minutes ago.",
    "Neutron stars can spin hundreds of times per second, sweeping beams of radiation past Earth like "
    "a lighthouse, we call those pulsars.",
    "The footprints left by astronauts on the Moon could last millions of years, because there's no "
    "wind or water to erase them.",
    "Most of the gold on Earth likely came from ancient collisions of neutron stars, forged in events "
    "across the galaxy long before the Sun existed.",
]
_GENERIC_JOKES = [
    # Dad jokes
    "I'd tell you a UDP joke, but you might not get it.",
    "There are 10 kinds of people: those who read binary, and those who don't.",
    "I only know 25 letters of the alphabet, I don't know y.",
    "I'm reading a book about anti-gravity. It's impossible to put down.",
    "Did you hear about the restaurant on the Moon? Great food, no atmosphere.",
    "I used to hate facial hair, but then it grew on me.",
    "Why don't scientists trust atoms? Because they make up everything.",
    "What do you call a fish wearing a bowtie? Sofishticated.",
    "I would tell you a chemistry joke, but I know I wouldn't get a reaction.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "I'm on a seafood diet. I see food and I eat it.",
    "What did the ocean say to the beach? Nothing, it just waved.",
    "Why can't you trust stairs? They're always up to something.",
    "I made a pencil with two erasers. It was pointless.",
    "How does the Moon cut its hair? Eclipse it.",
    "Parallel lines have so much in common. It's a shame they'll never meet.",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sig(text: str) -> str:
    norm = " ".join(text.lower().split())
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def _recent_sigs() -> set[str]:
    try:
        from ..db import get_app_state
        import json
        raw = get_app_state("delight_recent", "")
        return set(json.loads(raw)) if raw else set()
    except Exception:
        return set()


def _remember(sig: str) -> None:
    try:
        from ..db import get_app_state, set_app_state
        import json
        raw = get_app_state("delight_recent", "")
        lst = json.loads(raw) if raw else []
        lst.append(sig)
        lst = lst[-_RECENT_WINDOW:]
        set_app_state("delight_recent", json.dumps(lst))
    except Exception:
        pass


def _live_interests() -> list[str]:
    """Interest keys that are plausibly live for this user: the seeded set, lightly reordered by
    what shows up in memory_facts (so a user who talks a lot about cooking gets more cooking). Always
    returns the full seed set, memory only nudges ordering."""
    keys = list(_interests().keys())
    random.shuffle(keys)  # default: vary across interests rather than fixed order
    try:
        from .memory import all_facts
        blob = " ".join(f.get("fact", "").lower() for f in all_facts(limit=200))
        hint = _interest_hints()
        # stable-sort the already-shuffled list by score desc; equal scores keep random order
        scored = sorted(keys, key=lambda k: -sum(blob.count(h) for h in hint.get(k, ())))
        return scored
    except Exception:
        return keys


def _pick(pool: list[str], recent: set[str]) -> str | None:
    fresh = [t for t in pool if _sig(t) not in recent]
    if not fresh:
        fresh = pool  # everything's been seen recently; allow a repeat rather than nothing
    return random.choice(fresh) if fresh else None


def _model_nugget(kind: str, interest: str) -> str | None:
    """Ask the local model for a fresh fact/joke tied to an interest. Returns None if no local model
    is reachable. Caller labels it model-sourced. Best-effort, never blocks or raises."""
    topic = interest.replace("_", " ")
    if kind == "fact":
        prompt = (f"Give me ONE genuinely interesting, true fun fact about {topic}. One or two "
                  "sentences, warm and specific, no preamble. If you're not confident it's true, "
                  "pick a different fact you are confident about.")
    else:
        prompt = (f"Tell me ONE short, gentle, clever joke related to {topic}. One or two lines, "
                  "no preamble, genuinely amusing rather than groan-worthy if you can manage it.")
    try:
        import asyncio
        from ..llm import ask_persona
        try:
            asyncio.get_running_loop()
            return None  # already in an event loop; skip the model path to avoid nesting
        except RuntimeError:
            pass
        out = asyncio.run(ask_persona(prompt, role="chat"))
        return (out or "").strip() or None
    except Exception:
        return None


# --- public API --------------------------------------------------------------

def fun_fact(*, interest: str | None = None, allow_model: bool = True) -> dict[str, Any]:
    """A fun fact, ideally tied to one of Alex's interests. Tries the model tier (labelled
    model-sourced) when available and allowed; otherwise serves a shipped, hand-checked nugget.
    Honest about which it is via the 'source' field."""
    recent = _recent_sigs()
    interests = _interests()
    order = [interest] if interest and interest in interests else _live_interests()
    chosen_interest = order[0] if order else None

    # try the model first when allowed (fresh, but labelled as model-sourced / unverified)
    if allow_model and chosen_interest:
        m = _model_nugget("fact", chosen_interest)
        if m and _sig(m) not in recent:
            _remember(_sig(m))
            return {"text": m, "interest": chosen_interest, "source": "model",
                    "caveat": "model-generated, worth a sanity check before repeating as fact"}

    # shipped nuggets, walking interests in priority order, then generic
    for key in order:
        pick = _pick(interests.get(key, {}).get("facts", []), recent)
        if pick:
            _remember(_sig(pick))
            return {"text": pick, "interest": key, "source": "shipped"}
    pick = _pick(_GENERIC_FACTS, recent)
    if pick:
        _remember(_sig(pick))
        return {"text": pick, "interest": "general", "source": "shipped"}
    return {"text": "I'm fresh out of fresh facts for the moment.", "interest": None, "source": "none"}


def joke(*, interest: str | None = None, allow_model: bool = True) -> dict[str, Any]:
    """A light joke, ideally tied to an interest. Same honesty about sourcing as fun_fact."""
    recent = _recent_sigs()
    interests = _interests()
    order = [interest] if interest and interest in interests else _live_interests()
    chosen_interest = order[0] if order else None

    if allow_model and chosen_interest:
        m = _model_nugget("joke", chosen_interest)
        if m and _sig(m) not in recent:
            _remember(_sig(m))
            return {"text": m, "interest": chosen_interest, "source": "model"}

    for key in order:
        pick = _pick(interests.get(key, {}).get("jokes", []), recent)
        if pick:
            _remember(_sig(pick))
            return {"text": pick, "interest": key, "source": "shipped"}
    pick = _pick(_GENERIC_JOKES, recent)
    if pick:
        _remember(_sig(pick))
        return {"text": pick, "interest": "general", "source": "shipped"}
    return {"text": "My joke buffer needs a moment to refill.", "interest": None, "source": "none"}


def delight(*, kind: str = "auto", interest: str | None = None,
            allow_model: bool = True, audience: str = "general") -> dict[str, Any]:
    """One delight, a fact or a joke. kind: 'fact' | 'joke' | 'auto' (random).

    Draws from the scaled delight CACHE first (hundreds/thousands of autogenerated items, deduped),
    which is what lets delight scale well beyond the curated pool. Falls back to the curated shipped
    pool when the cache is empty (e.g. before any generation has run, or fully offline). audience
    selects the cache well: 'general' for the owner, 'guest' for the restricted guest surpriser, the
    guest well is neutral-only and never includes owner-personal material."""
    want_kind = None if kind == "auto" else kind
    # 1) Cache first (cheap, scaled).
    try:
        from. import delight_cache as _dc
        cached = _dc.draw(audience=audience, kind=want_kind, recent_sigs=_recent_sigs())
        if cached:
            _remember(cached["sig"])
            out = {"kind": cached["kind"], "text": cached["text"], "interest": "general",
                   "source": cached["source"]}
            if cached.get("caveat"):
                out["caveat"] = cached["caveat"]
            return out
    except Exception:
        pass
    # 2) Curated fallback (always available).
    if kind == "fact":
        return {"kind": "fact", **fun_fact(interest=interest, allow_model=allow_model)}
    if kind == "joke":
        return {"kind": "joke", **joke(interest=interest, allow_model=allow_model)}
    if random.random() < 0.5:
        return {"kind": "fact", **fun_fact(interest=interest, allow_model=allow_model)}
    return {"kind": "joke", **joke(interest=interest, allow_model=allow_model)}


def delight_for_voice(text: str = "") -> dict[str, Any]:
    """A spoken-shaped delight for the conversational brain. Reads the request for a hint of which
    register (joke vs fact) and which interest, then serves one warmly, no preamble, no 'here's a
    fun fact for you', just the thing."""
    low = (text or "").lower()
    kind = "joke" if ("joke" in low or "funny" in low or "laugh" in low) else (
        "fact" if ("fact" in low or "did you know" in low or "tell me something" in low) else "auto")
    # let an explicitly named interest through
    interest = None
    for key in _interests():
        if key.replace("_", " ") in low or key.split("_")[0] in low:
            interest = key
            break
    d = delight(kind=kind, interest=interest)
    speech = d["text"]
    # if it's a model-sourced 'fact', add a light honest tag (kept gentle, not a disclaimer wall)
    if d.get("kind") == "fact" and d.get("source") == "model":
        speech = speech.rstrip() + " (I'm fairly sure on that, but don't quote me in a pub quiz.)"
    return {"speech": speech, "kind": d.get("kind"), "interest": d.get("interest"),
            "source": d.get("source")}
