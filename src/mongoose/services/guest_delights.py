"""Guest delights: fun facts and little entertainments for paired guests.

When a guest (a friend like Sam) opens Mongoose, the experience should feel warm and a bit magical,
not just utilitarian. This module provides two things:

1. A curated, rotating library of FUN FACTS across many topics (gadgets, animals, chemistry, the
   universe, cars, culture, 1990s music, positive recent-ish news, AI, tech, Mongoose
   itself, and a few warm notes about Alex). Each fact carries a `style` tag so the UI can render
   different categories with different visual treatments (colour, emoji, accent).

2. A set of ENTERTAINING FUNCTIONS a guest can invoke when they ask "what can I do with you": small
   interactive bits (a fact on demand, a would-you-rather, a tiny riddle, a compliment, a mood lifter)
   that are safe, self-contained, and don't touch Alex's private data or the home controls.

Design notes:
- Everything here is SAFE FOR GUESTS: no owner memory, no home control, no private data. Facts are
  static and curated, not generated, so there's no hallucination surface and nothing leaks.
- Facts about Alex are deliberately light and public-spirited (things he'd be happy a friend sees),
  never private or sensitive.
- Selection is gently rotating + seedable so a guest doesn't see the same fact twice in a row, and so
  it's testable.
"""
from __future__ import annotations

import hashlib
import random
from typing import Any

# Each fact: (category, style, text). `style` drives the UI's visual treatment.
# Categories map to a colour/emoji accent the client picks up from the style tag.
_FACTS: list[tuple[str, str, str]] = [
    # gadgets / tech
    ("gadgets", "tech", "The first computer mouse, built by Doug Engelbart in the 1960s, was a carved block of wood with two metal wheels."),
    ("gadgets", "tech", "QWERTY keyboards were arranged to slow typists down so 19th-century typewriter arms wouldn't jam, and we just never changed it."),
    ("tech", "tech", "The '@' symbol in email was chosen in 1971 simply because it was on the keyboard and almost never used in names."),
    ("tech", "tech", "Wi-Fi doesn't stand for anything. It was a catchy brand name picked over the painfully literal 'IEEE 802.11b Direct Sequence'."),
    # animals
    ("animals", "nature", "A mongoose can take on a cobra because its acetylcholine receptors are shaped so snake venom barely grips them."),
    ("animals", "nature", "Octopuses have three hearts, blue blood, and can taste with their arms."),
    ("animals", "nature", "Wombats produce cube-shaped poop, which keeps it from rolling off the logs they use to mark territory."),
    ("animals", "nature", "A group of flamingos is called a 'flamboyance', which is the most accurate collective noun ever coined."),
    # chemistry
    ("chemistry", "science", "Glass isn't a slow-moving liquid. Medieval windows are thicker at the bottom because of how they were made, not because they flowed."),
    ("chemistry", "science", "Helium was discovered on the Sun (in its spectrum) before it was ever found on Earth, hence the name, from Helios."),
    ("chemistry", "science", "Gallium melts at about 30°C, so a spoon made of it will slowly disappear in a hot cup of tea."),
    # universe
    ("universe", "cosmos", "There are more possible games of chess than there are atoms in the observable universe."),
    ("universe", "cosmos", "A day on Venus is longer than its year, it rotates so slowly that one spin outlasts one orbit."),
    ("universe", "cosmos", "Neutron stars are so dense that a sugar-cube-sized piece would weigh about as much as Mount Everest."),
    # cars
    ("cars", "motor", "The Honda you might know is named after Ahura Honda, the Zoroastrian god of light, blended with founder Jujiro Matsuda's name."),
    ("cars", "motor", "The first speeding ticket was issued in 1896 to a man going 8 mph in a 2 mph zone."),
    # culture
    ("culture", "culture", "The 'OK' hand gesture and the word 'OK' likely come from a joking 1830s newspaper abbreviation of 'oll korrect'."),
    ("culture", "culture", "Ketchup was sold in the 1830s as medicine, marketed to cure indigestion and even diarrhoea."),
    # 1990s music
    ("music90s", "retro", "Radiohead's 'Creep' was nearly left off their 1993 debut, the band found it depressing; it became their breakout."),
    ("music90s", "retro", "The Macarena spent 14 weeks at US number one in 1996, the dance came after the song was already a hit."),
    ("music90s", "retro", "Nirvana's 'Smells Like Teen Spirit' was named after a deodorant a friend joked Kurt Cobain smelled of."),
    # positive news (evergreen-ish good news)
    ("goodnews", "joy", "Global extreme poverty has fallen by more than half since 1990, hundreds of millions of people lifted above the line."),
    ("goodnews", "joy", "The ozone layer is on track to fully recover by around 2066, a rare example of the whole world fixing something together."),
    ("goodnews", "joy", "Wild tiger numbers have started rising again after a century of decline, thanks to coordinated conservation."),
    # AI
    ("ai", "ai", "The word 'robot' comes from the Czech 'robota', meaning forced labour, from a 1920 play, not from engineering at all."),
    ("ai", "ai", "Early chatbots from the 1960s (like ELIZA) fooled people by just reflecting their words back as questions."),
    # Mongoose itself
    ("mongoose", "self", "Mongoose runs entirely on {{owner.name}}'s own machine, your chat with it never leaves the house. That's the whole point: a private brain."),
    ("mongoose", "self", "Under the hood Mongoose juggles a local language model, a search index, a home-control bridge, and a careful approval system so nothing acts without a human's say-so."),
    ("mongoose", "self", "Mongoose has a 'surpriser' that stays quiet unless it finds something genuinely worth telling you, it earns interruptions rather than nagging."),
    ("mongoose", "self", "The name fits: a mongoose is small, quick, fiercely loyal, and famously good at handling things far more dangerous than itself."),
    # The host (light, public-spirited). The category is the owner's first name, lowercased, and a site
    # profile can supply its own host notes (lexicon guest_delights.host_facts); see _facts().
    ("alex", "host", "Your host built Mongoose to be a thoughtful companion, so you're in good company."),
    ("alex", "host", "Your host builds things like this for fun, Mongoose is a hand-made, local-first assistant, not something bought off a shelf."),
    ("alex", "host", "Mongoose can chat in more than one language, so feel free to switch."),
    # ---- expanded pool (accuracy-checked) ----
    # space
    ("space", "space", "A day on Venus is longer than its year: it spins so slowly that one rotation takes about 243 Earth days, while its orbit takes 225."),
    ("space", "space", "It rains diamonds on Neptune and Uranus, where extreme pressure squeezes carbon in the atmosphere into solid gems."),
    ("space", "space", "Saturn's density is so low that if you could find a big enough bathtub, the whole planet would float."),
    ("space", "space", "Neutron stars are so dense that a sugar-cube-sized piece would weigh about a billion tonnes on Earth."),
    ("space", "space", "The Sun makes up about 99.86 percent of all the mass in the entire solar system."),
    ("space", "space", "Olympus Mons on Mars is the tallest known volcano in the solar system, nearly three times the height of Everest."),
    ("space", "space", "The footprints left by the Apollo astronauts will likely last millions of years, because there is no wind or water on the Moon to erase them."),
    ("space", "space", "Lightning is about five times hotter than the surface of the Sun."),
    # ocean and nature
    ("ocean", "nature", "We have better maps of the surface of Mars than of our own ocean floor: over 80 percent of the ocean has never been explored."),
    ("ocean", "nature", "A blue whale's heart is so large that its arteries are wide enough for a small child to crawl through."),
    ("nature", "nature", "Honey never spoils: archaeologists have found 3,000-year-old honey in Egyptian tombs still perfectly edible."),
    ("nature", "nature", "A single strand of spider silk is stronger than a strand of steel of the same thickness."),
    ("nature", "nature", "Trees share nutrients and warning signals with each other through underground fungal networks."),
    ("nature", "nature", "Sharks are older than trees, and older than Saturn's rings: they have swum the oceans for over 400 million years."),
    ("nature", "nature", "Sloths can hold their breath longer than dolphins, up to 40 minutes, by slowing their heart right down."),
    ("nature", "nature", "Cows have best friends and become stressed when they are kept apart from them."),
    ("nature", "nature", "A group of flamingos is called a flamboyance."),
    ("nature", "nature", "Bananas are slightly radioactive thanks to their potassium, though you would need to eat millions at once for it to matter."),
    ("nature", "nature", "A teaspoon of healthy soil contains more living organisms than there are people on Earth."),
    # human body and mind
    ("body", "body", "Your body makes roughly 25 million new cells every second."),
    ("body", "body", "The acid in your stomach could dissolve metal, but a fresh layer of mucus stops it from digesting you."),
    ("body", "body", "You are about a centimetre taller in the morning: a day of gravity gently compresses the cartilage in your spine."),
    ("body", "body", "By weight, bone is stronger than steel: a matchbox-sized block of it can bear around nine tonnes."),
    ("mind", "mind", "Your brain uses about 20 percent of your energy while being only about 2 percent of your weight."),
    ("mind", "mind", "Simply naming a feeling, saying 'I feel anxious', measurably calms the brain's stress response."),
    ("mind", "mind", "You cannot tickle yourself, because your brain predicts your own movements and cancels the surprise."),
    ("mind", "mind", "The maddening sense that a word is on the tip of your tongue has its own name: lethologica."),
    ("mind", "mind", "Humans appear to be the only animals that blush."),
    # history
    ("history", "history", "Oxford University is older than the Aztec Empire: teaching began at Oxford around 1096, and Tenochtitlan was founded in 1325."),
    ("history", "history", "Cleopatra lived closer in time to the Moon landing than to the building of the Great Pyramid."),
    ("history", "history", "The shortest war in history, between Britain and Zanzibar in 1896, lasted under 40 minutes."),
    ("history", "history", "The Hundred Years' War actually ran for 116 years."),
    ("history", "history", "In 1518 a 'dancing plague' gripped Strasbourg, where hundreds of people danced uncontrollably for days."),
    ("history", "history", "Napoleon was once mobbed by a horde of tame rabbits at a hunt arranged in his honour."),
    ("history", "history", "The first webcam was set up at Cambridge in 1991 to watch a coffee pot, so nobody walked over to find it empty."),
    # words and language
    ("language", "words", "The dot over a lowercase i or j has a name: it is called a tittle."),
    ("language", "words", "'Bookkeeper' is one of very few common English words with three consecutive double letters."),
    ("language", "words", "The word 'nice' once meant foolish or ignorant, from the Latin nescius."),
    ("language", "words", "Shakespeare is the first recorded user of over a thousand English words, among them 'eyeball', 'lonely', and 'gossip'."),
    ("language", "words", "Polish has seven grammatical cases, which is why its word endings shift around so much."),
    # food
    ("food", "food", "Carrots were originally purple; the familiar orange kind was cultivated later in the Netherlands."),
    ("food", "food", "Peanuts are not nuts at all: they are legumes, and they grow underground like peas."),
    ("food", "food", "A strawberry is botanically not a berry, while a banana is."),
    ("food", "food", "A pineapple plant takes about two years to grow a single fruit."),
    # places
    ("geography", "places", "Russia spans 11 time zones, so it can be breakfast at one end and bedtime at the other."),
    ("geography", "places", "Australia is wider than the Moon: nearly 4,000 km across, against the Moon's 3,400 km diameter."),
    ("geography", "places", "Canada has more lakes than the rest of the world combined."),
    ("geography", "places", "The Sahara was green, with lakes and grassland, as recently as about 6,000 years ago."),
    # science and everyday
    ("science", "science", "Hot water can sometimes freeze faster than cold, an effect named after a Tanzanian schoolboy, Erasto Mpemba."),
    ("science", "science", "There are more ways to shuffle a deck of 52 cards than there are atoms making up the Earth."),
    ("science", "science", "The Eiffel Tower grows about 15 cm taller in summer as its iron expands in the heat."),
    ("tech", "tech", "The average smartphone has far more computing power than all of NASA had for the 1969 Moon landing."),
    ("tech", "tech", "The 'save' icon in most software is a floppy disk, a device many people using it have never actually held."),
    ("misc", "misc", "A 'jiffy' is a real unit of time: in physics it can mean how long light takes to travel one centimetre, about 33 trillionths of a second."),
    ("misc", "misc", "The inventor of the frisbee was, at his request, cremated and moulded into frisbees after he died."),
]

# Entertaining interactive bits for the "what can I do with you" menu.
_WOULD_YOU_RATHER = [
    "Would you rather have a pet dragon the size of a cat, or a cat the size of a dragon?",
    "Would you rather always know when someone is lying, or always get away with lying yourself?",
    "Would you rather explore deep space or the deep ocean?",
    "Would you rather have unlimited free travel or unlimited free food, forever?",
]
_RIDDLES = [
    ("I speak without a mouth and hear without ears. I have no body, but I come alive with wind. What am I?", "An echo."),
    ("The more you take, the more you leave behind. What am I?", "Footsteps."),
    ("What has keys but can't open locks, space but no room, and you can enter but can't go inside?", "A keyboard."),
]
_COMPLIMENTS = [
    "Whoever set this up clearly has good taste in friends, because here you are.",
    "You ask good questions. That's rarer than it sounds.",
    "You've got the kind of curiosity that makes good things happen.",
]
_MOOD_LIFTERS = [
    "Somewhere right now, a dog is being told it's a good boy. The universe is, on balance, alright.",
    "Otters hold hands while they sleep so they don't drift apart. Be like otters.",
    "There are more trees on Earth than stars in the Milky Way. Go say hi to one.",
]


def _facts() -> list[tuple[str, str, str]]:
    """The fact library as this site serves it. Placeholders such as {{owner.name}} are filled from the
    site profile; the host notes come from profile lexicon guest_delights.host_facts when present (their
    category is the owner's name, lowercased); and lexicon guest_delights.fact_swaps maps a shipped fact's
    text to the site's own version of it."""
    from .. import profile as _profile
    host_key = _profile.owner_name().lower()
    host_texts = _profile.lexicon("guest_delights.host_facts", None)
    swaps = _profile.lexicon("guest_delights.fact_swaps", {}) or {}
    out: list[tuple[str, str, str]] = []
    host_done = False
    for cat, style, text in _FACTS:
        if style == "host":
            if host_texts is None:
                out.append((host_key, style, _profile.render(text)))
            elif not host_done:
                out.extend((host_key, style, str(t)) for t in host_texts)
                host_done = True
            continue
        out.append((cat, style, _profile.render(swaps.get(text, text))))
    return out


def _seeded(seq, seed: str | None):
    if seed is None:
        return random.choice(seq)
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return seq[h % len(seq)]


def random_fact(*, category: str | None = None, exclude: str | None = None,
                seed: str | None = None) -> dict[str, Any]:
    """A fun fact, optionally filtered to a category, optionally avoiding a given text (so a guest
    doesn't see the same one twice in a row). Returns text + a style tag for the UI."""
    facts = _facts()
    pool = [f for f in facts if (category is None or f[0] == category)]
    if exclude:
        filtered = [f for f in pool if f[2] != exclude]
        if filtered:
            pool = filtered
    if not pool:
        pool = facts
    cat, style, text = _seeded(pool, seed)
    return {"category": cat, "style": style, "text": text}


def categories() -> list[str]:
    """All available fact categories (for a 'surprise me about X' picker)."""
    seen = []
    for c, _, _ in _facts():
        if c not in seen:
            seen.append(c)
    return seen


def entertain(kind: str = "", *, seed: str | None = None) -> dict[str, Any]:
    """An entertaining bit for the guest. kind: fact | would_you_rather | riddle | compliment |
    mood | menu (default lists what's available)."""
    kind = (kind or "menu").strip().lower()
    if kind in ("fact", "funfact", "surprise"):
        return {"kind": "fact", **random_fact(seed=seed)}
    if kind in ("would_you_rather", "wyr"):
        return {"kind": "would_you_rather", "text": _seeded(_WOULD_YOU_RATHER, seed)}
    if kind == "riddle":
        q, a = _seeded(_RIDDLES, seed)
        return {"kind": "riddle", "question": q, "answer": a}
    if kind in ("compliment", "nice"):
        return {"kind": "compliment", "text": _seeded(_COMPLIMENTS, seed)}
    if kind in ("mood", "cheer", "lift"):
        return {"kind": "mood", "text": _seeded(_MOOD_LIFTERS, seed)}
    # menu
    return {
        "kind": "menu",
        "intro": "Here's what we can do for fun:",
        "options": [
            {"id": "fact", "label": "Tell me a fun fact", "emoji": "💡"},
            {"id": "would_you_rather", "label": "Would you rather…", "emoji": "🤔"},
            {"id": "riddle", "label": "Give me a riddle", "emoji": "🧩"},
            {"id": "compliment", "label": "Say something nice", "emoji": "✨"},
            {"id": "mood", "label": "Cheer me up", "emoji": "🦦"},
        ],
        "topics": categories(),
    }
