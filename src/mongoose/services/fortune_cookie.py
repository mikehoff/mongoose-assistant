"""Fortune cookie cards: a delightful little feature. "generate fortune cookie" (for the asker) or
"make Jo a fortune" produces an A5 landscape PDF (cream background, navy bold serif, colour emoji,
centred) with a fanciful Chinese-fortune-cookie style line, and returns a download link. Works in
private and public (lounge) chat. It renders via a tiny Swift/Core Text card renderer (the only way to
get full colour emoji plus arbitrary fonts into a PDF on the Mac; reportlab cannot do colour emoji).
"""
from __future__ import annotations

import random
import subprocess
from pathlib import Path
from typing import Any

_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "fortune"
_SWIFT_CARD = _ASSETS / "fortune_card.swift"

# Fanciful fortune-cookie lines: motivation on goals, gym, work, dreams, the future, plus a few kooky
# ones. No two-em-dash artefacts, no leading capital fuss: these are the cookie's whole voice.
_FORTUNES = [
    "A goal whispered to the morning becomes a habit by nightfall.",
    "The barbell remembers every rep you skipped, and forgives none of them.",
    "Your future self is already proud; do not keep them waiting.",
    "Small disciplines, stacked daily, topple mountains by spring.",
    "The dream you keep rehearsing is auditioning for reality.",
    "Today's sweat is tomorrow's armour.",
    "Work quietly on the thing that scares you; it is scared of you too.",
    "The strongest lift is the one you almost skipped.",
    "Fortune favours the one who shows up on the boring days.",
    "A river carved the canyon not by force but by returning every day.",
    "Your goals are not too big; your excuses are just loud.",
    "Rest is a strategy, not a surrender.",
    "The gym is a mirror that only shows the future.",
    "Plant the seed of a wild idea; water it before you doubt it.",
    "Momentum is shy: greet it early and it stays all day.",
    "You are three good mornings away from a different life.",
    "The comfort zone is a lovely place where nothing ever grows.",
    "Chase the dream loudly enough that your fears lose the argument.",
    "A single push-up beats a perfect plan you never start.",
    "Your best work is hiding one uncomfortable week away.",
    "The stars align for those who train in the dark.",
    "Doubt is just excitement that forgot its manners.",
    "Build the body, and the mind moves in for free.",
    "Great futures are assembled from ordinary Tuesdays.",
    "The lucky ones are simply the ones who kept going.",
    "A kooky idea today is a legendary story tomorrow.",
    "Dance like the deadline is watching, work like it is not.",
    "Somewhere, your future high-five is already warming up its hand.",
    # Wider register: whimsical, tender, cosmic, wry, mysterious , so it does not read as gym-poster only.
    "A stranger will laugh at your joke this week; keep it loaded.",
    "The thing you lost is behind the thing you never move.",
    "Your name sounds different in a language you have not learned yet.",
    "Be suspicious of any Tuesday that goes exactly to plan.",
    "The cat was right about you all along.",
    "Someone is keeping a small kindness of yours in their pocket like a warm stone.",
    "The moon has seen your worst nights and still shows up.",
    "You will misremember today fondly, so live it slightly better.",
    "An old song will ambush you and you will let it.",
    "The door you have been knocking on opens inward.",
    "Trust the idea that visits you in the shower and refuses to leave.",
    "A quiet person is about to change your whole week with one sentence.",
    "The universe is not subtle; you have just been polite about noticing.",
    "Your future is eavesdropping, so speak kindly about it.",
    "The best map is a friend who has been lost in the same place.",
    "Something you buried is composting into something you will need.",
    "A small animal will make your day and ask for nothing.",
    "Say the ridiculous thing; the sensible ones are all taken.",
    "The tide does not apologise for leaving, and it always comes back.",
    "You are allowed to want the enormous, embarrassing version of your life.",
    "A letter you never sent is still doing quiet work somewhere.",
    "Luck is a rumour you can start about yourself.",
    "The stars are just old light being stubborn; so are you.",
    "Forgive the version of you that did not know yet.",
    "Someone will offer you tea at exactly the right moment; take it.",
    "Your weird is the part they will miss most.",
]

# the owner's requested garnish set (skin-tone modifiers kept where given).
_EMOJI = ["🔮", "🥠", "✨", "🪄", "🫨", "🌝", "🌛", "🙊", "🙈", "🌟", "⭐", "⚡", "💫",
          "💌", "💟", "👐🏼", "🤜🏼", "🙏🏼", "💅🏼"]


def _emoji_line(rng: random.Random) -> str:
    """A short decorative emoji line, e.g. '🥠 ✨ 🔮 💫 🙏🏼'."""
    n = rng.randint(3, 5)
    return " ".join(rng.sample(_EMOJI, n))


def _fsig(s: str) -> str:
    import hashlib
    return hashlib.sha1(" ".join((s or "").lower().split()).encode()).hexdigest()[:16]


def _recent_fortunes() -> set[str]:
    try:
        from ..db import get_app_state
        import json
        raw = get_app_state("fortune_recent", "")
        return set(json.loads(raw)) if raw else set()
    except Exception:
        return set()


def _remember_fortune(sig: str) -> None:
    try:
        from ..db import get_app_state, set_app_state
        import json
        raw = get_app_state("fortune_recent", "")
        lst = json.loads(raw) if raw else []
        lst.append(sig)
        set_app_state("fortune_recent", json.dumps(lst[-400:]))
    except Exception:
        pass


def _model_fortune() -> str | None:
    """Ask the local model for ONE surprising, soulful fortune line, for genuine variety (the curated
    pool alone reads repetitive fast). Best-effort: returns None if no model is reachable or we are on an
    event loop (must not nest asyncio.run). Callers reach this via asyncio.to_thread, so normally a
    worker thread with no running loop, and the model path runs."""
    try:
        import asyncio
        from ..llm import ask_persona
        try:
            asyncio.get_running_loop()
            return None
        except RuntimeError:
            pass
        prompt = ("Write ONE fortune-cookie line and nothing else: a single sentence of 8 to 18 words, "
                  "surprising and a little soulful or wry, never generic hustle or gym advice. No quotes, "
                  "no preamble, no emoji. Make it feel written for a real person's odd, hopeful evening.")
        out = asyncio.run(ask_persona(prompt, role="chat"))
        line = (out or "").strip().strip('"').splitlines()[0].strip() if out else ""
        # Reject junk/over-long model output; keep it cookie-shaped.
        if line and 4 <= len(line.split()) <= 26 and len(line) <= 160:
            return line
        return None
    except Exception:
        return None


def pick_fortune(seed: int | None = None, *, allow_model: bool = True) -> dict[str, Any]:
    """Choose a fortune line and an emoji garnish. Returns {line, emoji, card_text}.

    Normal path (seed is None): try a fresh model-written line for soul/surprise, else a curated line,
    both deduped against the last ~400 served so it does not repeat. A caller passing an explicit seed
    (e.g. a test) gets the old deterministic curated pick with no model call or dedup."""
    rng = random.Random(seed)
    if seed is not None:
        line = rng.choice(_FORTUNES)
        emoji = _emoji_line(rng)
        return {"line": line, "emoji": emoji, "card_text": f"{line}\n\n{emoji}"}
    recent = _recent_fortunes()
    line = None
    if allow_model:
        m = _model_fortune()
        if m and _fsig(m) not in recent:
            line = m
    if not line:
        fresh = [f for f in _FORTUNES if _fsig(f) not in recent] or _FORTUNES
        line = rng.choice(fresh)
    _remember_fortune(_fsig(line))
    emoji = _emoji_line(rng)
    return {"line": line, "emoji": emoji, "card_text": f"{line}\n\n{emoji}"}


def _swift() -> str | None:
    """Resolve swift without depending on the launchd PATH (reuses the OCR resolver, /usr/bin fallback)."""
    try:
        from .ocr import _tool_path
        p = _tool_path("swift")
        if p:
            return p
    except Exception:
        pass
    for cand in ("/usr/bin/swift", "/usr/local/bin/swift"):
        if Path(cand).exists():
            return cand
    import shutil
    return shutil.which("swift")


def _bundled_font() -> str:
    """A Garamond .ttf/.otf dropped into the assets font dir is used verbatim; else '' (serif fallback)."""
    for pat in ("*.ttf", "*.otf"):
        for f in sorted(_ASSETS.glob(pat)):
            if "garamond" in f.name.lower():
                return str(f)
    return ""


def _out_dir() -> Path:
    from ..settings import get_settings
    d = get_settings().db_path.parent / "fortunes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def render_card(card_text: str, out_path: str | Path) -> dict[str, Any]:
    """Render the A5 landscape fortune PDF via the Swift/Core Text card renderer. Returns {ok, path,
    font} or an honest failure (swift missing / render error)."""
    swift = _swift()
    if not swift:
        return {"ok": False, "reason": "no_swift",
                "hint": "The card renderer needs Swift (Xcode Command Line Tools: xcode-select --install)."}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [swift, str(_SWIFT_CARD), str(out_path), card_text]
    font_file = _bundled_font()
    if font_file:
        cmd.append(font_file)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "render_timeout"}
    except Exception as exc:
        return {"ok": False, "reason": f"render_error: {exc}"}
    if r.returncode != 0 or not out_path.exists():
        return {"ok": False, "reason": "render_failed", "detail": (r.stderr or r.stdout or "")[-300:]}
    return {"ok": True, "path": str(out_path), "font": (r.stdout or "").strip() or "serif"}


def _safe(s: Any) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s or "you")).strip("_") or "you"


def generate(recipient: str = "", seed: int | None = None, download_ttl_s: int = 365 * 24 * 3600) -> dict[str, Any]:
    """Pick a fortune, render the card, mint a download link. recipient is only used to name the file and
    the reply (the card itself stays classic-cookie impersonal). Returns {ok, download_url, filename,
    line, font, recipient} or an honest failure."""
    f = pick_fortune(seed)
    who = _safe(recipient) if recipient else "you"
    out = _out_dir() / f"fortune_{who}_{abs(hash(f['card_text'])) % 100000}.pdf"
    r = render_card(f["card_text"], out)
    if not r.get("ok"):
        return r
    from .rtm_templates import mint_download
    token = mint_download(r["path"], f"Fortune_{who}.pdf", ttl_s=download_ttl_s, media_type="application/pdf")
    return {"ok": True, "download_url": f"/rtm/doc/{token}", "download_token": token,
            "filename": f"Fortune_{who}.pdf", "line": f["line"], "emoji": f["emoji"],
            "font": r.get("font"), "recipient": recipient}


# --------------------------------------------------------------------------- chat bridge

def _parse_recipient(text: str) -> str:
    """Who is the fortune for. '' means the asker (self). 'for me' / 'my fortune' are explicit self;
    otherwise a name after for/make/give/gift, or 'Jo's fortune'."""
    import re
    t = text or ""
    if re.search(r"\bfor\s+me\b|\bmy\s+own\b|\bmyself\b", t, re.I):
        return ""
    for pat in (r"\bfor\s+([A-Za-z][\w'\-]+)",
                r"\b(?:make|give|gift|generate|create)\s+([A-Za-z][\w'\-]+)\s+(?:a\s+|the\s+|another\s+)?fortune",
                r"\b([A-Za-z][\w'\-]+)'s\s+fortune"):
        m = re.search(pat, t, re.I)
        if m:
            cand = re.sub(r"'s$", "", m.group(1))  # 'Susan's' -> 'Susan', keep 'Chris' intact
            if cand.lower() in {"me", "a", "an", "the", "my", "cookie", "fortune", "another", "chinese",
                                "kooky", "chat", "us", "everyone", "someone", "please"}:
                continue
            return cand[0].upper() + cand[1:]
    return ""


def _abs(url: str) -> str:
    if not url:
        return url
    try:
        from .remote_access import public_base_url
        base = public_base_url()
        if base:
            return base.rstrip("/") + url
    except Exception:
        pass
    return url


def maybe_handle_fortune(text: str, sender_name: str = "", is_owner: bool = False) -> dict[str, Any] | None:
    """Produce a fortune card and a reply with a download link. Runs the Swift renderer (a subprocess),
    so callers on the event loop should invoke this via asyncio.to_thread. Returns {reply, source} or a
    graceful failure message. Never raises for a normal request."""
    recipient = _parse_recipient(text)
    res = generate(recipient=recipient)
    if not res.get("ok"):
        why = res.get("hint") or res.get("reason") or "something went wrong"
        return {"reply": f"I couldn't bake that fortune cookie: {why}", "source": "fortune"}
    link = _abs(res["download_url"])
    # Do NOT reveal the fortune line in chat: the surprise lives inside the card. Just hand over the
    # sealed cookie with a tappable link.
    if recipient:
        head = f"🥠 A fortune for {recipient} is sealed and ready. Crack it open:"
    elif sender_name and not is_owner:
        head = f"🥠 Your fortune is sealed, {sender_name}. Crack it open:"
    else:
        head = "🥠 Your fortune is sealed inside. Crack it open:"
    reply = f"{head}\n{link}"
    return {"reply": reply, "source": "fortune", "download_url": res["download_url"],
            "filename": res["filename"], "recipient": recipient}
