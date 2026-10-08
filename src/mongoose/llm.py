from __future__ import annotations

from typing import Any

import asyncio
import re
import threading
import time
import weakref

import httpx

from .persona import persona_card as _persona_card
from .settings import get_settings

# The base M4 Mac mini runs ONE model at a time. If a chat turn, a post-reply fact extraction, and a
# scheduler job all hit Ollama at once, the box thrashes (load/evict/reload a multi-GB model) and can
# lock up. We serialise ALL local inference so calls queue instead of colliding.
#
# This must be cross-event-loop, not merely one asyncio.Lock per loop: scheduler watchdog hardening
# may run due jobs inside worker threads with their own event loops. A per-loop lock would let a
# scheduler model call collide with a chat model call. The tiny async context below acquires a
# process-wide threading lock without blocking the running event loop while waiting.
_INFERENCE_MUTEX = threading.Lock()


class _CrossLoopInferenceLock:
    async def __aenter__(self):
        await asyncio.to_thread(_INFERENCE_MUTEX.acquire)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        _INFERENCE_MUTEX.release()
        return False


def _inference_lock() -> _CrossLoopInferenceLock:
    return _CrossLoopInferenceLock()


# --- Per-conversation turn lock ------------------------------------------------------------------
# _INFERENCE_MUTEX (above) serialises MODEL INFERENCE only. It does NOT stop two messages on the SAME
# conversation from running their WHOLE turn (read history -> route/interpret -> execute -> persist)
# concurrently: both would read the same stale history, the model calls would serialise on the mutex,
# but the persists would interleave and the second turn's tool/route decision would be made without the
# first's reply in context. On the shared owner thread (DEFAULT_CONVERSATION_ID, used by phone + Mac +
# /assist) two messages sent close together therefore came back crossed / out of order.
#
# Fix: a per-conversation asyncio.Lock. A whole turn for a given conversation id is serialised end to
# end; turns for DIFFERENT conversation ids (each guest thread has its own id) still run fully in
# parallel, so a guest is never blocked by the owner's turn and vice versa.
#
# LOCK ORDERING (deadlock-freedom): the turn lock is COARSER than the inference mutex and is ALWAYS
# acquired FIRST (outer). The inference mutex is only ever taken deeper in the stack, inside the actual
# model call (_chat_call / ask_ollama), never around a turn lock. A holder of the turn lock may block
# waiting for the inference mutex, but a holder of the inference mutex never waits for a turn lock, so
# the two can never form a cycle. The turn lock is unbounded (asyncio.Lock has no timeout); that is safe
# because a turn always runs to completion and releases via `async with` even on error: a model timeout
# raises LocalModelError and unwinds the context, freeing the lock. chat_with_memory is never re-entrant
# for the same conversation id (no handler it calls loops back into chat_with_memory), so a turn cannot
# wait on the lock it already holds.
#
# The registry is keyed by the RUNNING event loop (weakly, so a finished loop's locks are GC'd) and then
# by conversation id. Keying by loop matters because asyncio.Lock is bound to the loop that first awaits
# it: in this single-process app all chat turns run on the main loop and share the same locks (real
# serialisation), while a test's repeated asyncio.run() or a scheduler worker thread's private loop gets
# its own locks instead of crashing on a cross-loop reuse. Turns on different loops cannot truly overlap
# on shared in-memory state anyway (asyncio.run blocks), so per-loop locks are correct, not weaker.
_TURN_LOCKS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()  # loop -> {conv_id: asyncio.Lock}
_TURN_LOCKS_GUARD = threading.Lock()


def _turn_lock_for(conversation_id: str) -> asyncio.Lock:
    """Return the per-conversation turn lock for the CURRENT event loop, creating it lazily. The tiny
    dict mutation is guarded by a threading.Lock so concurrent loops/threads cannot race the create."""
    loop = asyncio.get_running_loop()
    key = conversation_id or "_default_"
    with _TURN_LOCKS_GUARD:
        per_loop = _TURN_LOCKS.get(loop)
        if per_loop is None:
            per_loop = {}
            _TURN_LOCKS[loop] = per_loop
        lock = per_loop.get(key)
        if lock is None:
            lock = asyncio.Lock()
            per_loop[key] = lock
        return lock

# Logical roles map to models in settings.model_for_role.
#   chat -> persona/default (Gemma 26B): the voice the user hears (the owner)
#   tool -> tool-routing/JSON planning: defaults to the chat model (Gemma emitted
#           correct tool JSON in testing without a separate model + swap cost)
#   code -> coding (Qwen-coder), loaded on demand; a silent worker
#   research -> silent research worker (e.g. qwen3.5:9b); returns evidence, never
#           speaks to the user. Internal-only: not user-requestable as a speaker.
VALID_ROLES = {"chat", "tool", "code", "research"}
# Roles a CALLER may request directly via the API. "research" is deliberately NOT
# here: it is a SILENT worker role used internally (by the research tool), never a
# user-facing speaker. Letting a caller request role="research" would return the
# worker model's raw output as the reply, bypassing the one-speaker coherence rule.
# "code" is also internal-only as a speaker (it's a worker). The owner speaks.
USER_REQUESTABLE_ROLES = {"chat", "tool"}




def _ollama_timeout(settings) -> float:
    try:
        return max(1.0, float(getattr(settings, "ollama_request_timeout_s", 120.0)))
    except (TypeError, ValueError):
        return 120.0


def _ollama_options(settings) -> dict[str, int]:
    opts = {"num_ctx": int(getattr(settings, "num_ctx", 8192))}
    try:
        n = int(getattr(settings, "ollama_num_predict", 800))
    except (TypeError, ValueError):
        n = 800
    # Output-token cap: the model can still be clever, but it cannot loop forever generating text.
    # Keep a broad upper bound so an env typo cannot request effectively unbounded output.
    if n > 0:
        opts["num_predict"] = max(1, min(n, 4096))
    return opts


class LocalModelError(RuntimeError):
    """Raised when the local model (Ollama) cannot be reached or returns an error.

    Carries a short, user-facing message so callers can show something friendly
    instead of a raw traceback.
    """

    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(detail or user_message)
        self.user_message = user_message
        self.detail = detail


async def ask_ollama(
    prompt: str,
    system: str | None = None,
    *,
    role: str = "chat",
    model: str | None = None,
    response_format: Any = None,
) -> str:
    """Send a single-turn chat request to Ollama for a given logical role.

    `model` overrides the role mapping when set. On a 24 GB base M4 the
    chat/tool/code models are mutually exclusive in memory, so keep_alive is
    sent explicitly to let one role's model unload before another loads.
    `response_format` (e.g. "json") sets Ollama's `format`; kept light because a
    full nested JSON schema constraint can stall Gemma. Callers share the same
    inference lock, so routing this through here avoids contending with the
    server's own model calls.
    """
    settings = get_settings()
    if role not in VALID_ROLES:
        role = "chat"
    selected = model or settings.model_for_role(role)

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": selected,
        "messages": messages,
        "stream": False,
        "think": settings.ollama_think,
        "keep_alive": settings.keep_alive_value,
        "options": _ollama_options(settings),
    }
    if response_format is not None:
        payload["format"] = response_format

    timeout_s = _ollama_timeout(settings)
    async with _inference_lock():
      async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            response = await asyncio.wait_for(
                client.post(
                    f"{settings.ollama_base_url.rstrip('/')}/api/chat",
                    json=payload,
                ),
                timeout=timeout_s,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            # A 404 here usually means the selected model isn't pulled. If we
            # weren't already on the chat model, retry once with it, since the
            # chat model is the one most likely to be present. This rescues
            # prompts that the router sent to an un-pulled tool/code model.
            chat_model = settings.ollama_chat_model
            if exc.response.status_code == 404 and selected != chat_model and model is None:
                try:
                    payload["model"] = chat_model
                    retry = await asyncio.wait_for(
                        client.post(
                            f"{settings.ollama_base_url.rstrip('/')}/api/chat",
                            json=payload,
                        ),
                        timeout=timeout_s,
                    )
                    retry.raise_for_status()
                    data = retry.json()
                except httpx.HTTPStatusError:
                    raise LocalModelError(
                        f"The model for this request ({selected}) isn't pulled. "
                        f"Run `ollama pull {selected}` or `mongoose doctor`.",
                        detail=f"HTTP {exc.response.status_code} from {exc.request.url}",
                    ) from exc
                else:
                    message = data.get("message", {})
                    content = (message.get("content") or "").strip()
                    if not content:
                        content = (message.get("thinking") or "").strip()
                    return content
            raise LocalModelError(
                f"The local model ({selected}) returned an error. Try `mongoose doctor`, "
                "check `ollama ps`, and confirm the model is pulled.",
                detail=f"HTTP {exc.response.status_code} from {exc.request.url}",
            ) from exc
        except (asyncio.TimeoutError, httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            raise LocalModelError(
                "Can't reach the local model. Is Ollama running? Try `ollama serve` "
                "and `mongoose doctor`.",
                detail=str(exc),
            ) from exc
        message = data.get("message", {})
        content = (message.get("content") or "").strip()
        # Some thinking-mode models occasionally leave content empty and put the
        # answer in `thinking`. Fall back so Mongoose never returns silence.
        if not content:
            content = (message.get("thinking") or "").strip()
        return content


def _dedash(text: str) -> str:
    """Strip em/en dashes from what Mongoose SAYS. the owner's no-dash rule is about the product's voice, not
    just source code, and the local model emits em dashes constantly ('so far, is it'). Applied to
    user-facing model prose only (ask_persona + the main reply), never to the raw classifier path where
    output is parsed as JSON/labels. Uses plain punctuation, and never introduces a space-before-comma."""
    if not text or ("—" not in text and "–" not in text and "―" not in text):
        return text
    import re as _re
    text = _re.sub(r"(\d)\s*[–—]\s*(\d)", r"\1-\2", text)   # number ranges -> hyphen
    text = _re.sub(r"\s*[—―]\s*", ", ", text)              # em-dash clause break -> ", "
    text = _re.sub(r"\s*–\s*", ", ", text)                      # stray en-dash -> ", "
    return text


# Financial anti-fabrication net. The local model once INVENTED an invoice's bank details (sort 00-00-00 /
# account 12345678, textbook placeholders, none in the real PDF) because it was asked to "open" a document
# it couldn't actually read. read_document now lets it read the real file; this is the belt-and-braces catch
# so no reply that asserts bank details can ever be paid against unverified. Only these tools return a
# document's ACTUAL text, so only they count as "grounded".
_SORT_CODE_RE = re.compile(r"\b\d{2}[-\s]\d{2}[-\s]\d{2}\b")
_ACCOUNT_RE = re.compile(r"(?i)\baccount\s*(?:number|no\.?|#)?\s*[:\-]?\s*\d{8}\b")
_DOC_CONTENT_TOOLS = {"read_document", "retrieve_file", "retrieve_attachment"}


def _bank_details_note(text: str, *, grounded: bool) -> str:
    """Deterministic BACKSTOP only (last resort). Returns a caveat to append if a reply asserts bank details
    (both a sort code and an 8-digit account), else ''. Used by _bank_details_reflect when the model fails to
    phrase its own caution or alters the figures, so financial safety never depends solely on reflection."""
    if not text or not (_SORT_CODE_RE.search(text) and _ACCOUNT_RE.search(text)):
        return ""
    if grounded:
        return ("\n\n(Before paying: confirm these bank details by phone with the payee on a number you "
                "already hold, especially for a first payment.)")
    return ("\n\n(Caution: I did not read these bank details from the actual document, so they could be "
            "wrong. Do not pay against them. Ask me to read the invoice itself, then confirm by phone before "
            "you pay.)")


_CAUTION_CUE_RE = re.compile(r"(?i)\b(confirm|verif|check|call|phone|do ?n['o]t pay|read the|open the|unverified|placeholder)\b")


def _is_placeholder_number(raw: str) -> bool:
    """True if a sort code / account number is a textbook placeholder: all-same-digit (00-00-00, 11111111)
    or a run of consecutive digits (12345678, 87654321). Cheap sanity check so an extracted 00-00-00 /
    12345678 is never presented with the same confidence as a real account."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) < 6:
        return False
    if len(set(digits)) == 1:
        return True
    asc = "01234567890123456789"
    if digits in asc or digits in asc[::-1]:
        return True
    return False


def _implausible_bank_details(text: str) -> bool:
    """True if the sort code OR account number in a reply is a placeholder pattern (see above)."""
    m_sort = _SORT_CODE_RE.search(text or "")
    m_acct = _ACCOUNT_RE.search(text or "")
    return bool((m_sort and _is_placeholder_number(m_sort.group(0)))
                or (m_acct and _is_placeholder_number(m_acct.group(0))))


async def _bank_details_reflect(reply: str, *, grounded: bool) -> str:
    """the owner's always-think principle applied to the financial net: instead of stapling a fixed caveat, let the
    MODEL reflect, given whether it actually read the source, and phrase its own verification caution in its
    own voice. The deterministic _bank_details_note fires ONLY as a silent backstop, when the reflection drops
    the caution or changes a figure, so safety never rests on the reflection alone. No bank details -> the
    reply passes through untouched (no extra model call, no noise)."""
    m_sort = _SORT_CODE_RE.search(reply or "")
    m_acct = _ACCOUNT_RE.search(reply or "")
    if not (m_sort and m_acct):
        return reply
    sort_txt, acct_txt = m_sort.group(0), m_acct.group(0)
    if _is_placeholder_number(sort_txt) or _is_placeholder_number(acct_txt):
        # A textbook placeholder (00-00-00 / 12345678) is never a real account, whatever the "grounded"
        # flag says. Treat as definitely-not-payable and tell the owner exactly why.
        fact = ("These bank details are a PLACEHOLDER pattern (all-same-digit or sequential), which is never "
                "a real bank account. Tell the owner plainly that these are not genuine details, so extraction "
                "likely failed or the document is suspect, and NOT to pay against them. Offer to re-read the "
                "actual invoice.")
        sample = "Tone (do not copy): these look like placeholder numbers, not a real account, so do not pay them."
    elif grounded:
        fact = ("You DID read these bank details from the actual document this turn, so they are as printed. "
                "Still tell the owner to confirm them by phone before a first payment, in your own words.")
        sample = "Tone (do not copy): worth a 30-second call to the payee to confirm before you send a first payment."
    else:
        fact = ("You did NOT read these bank details from any document this turn. They are UNVERIFIED and could "
                "be wrong or invented. Tell the owner plainly not to pay against them yet, and offer to read the "
                "actual invoice, in your own words.")
        sample = ("Tone (do not copy): I have not checked these against the real invoice, so do not pay them yet. "
                  "Let me open the document first.")
    try:
        revised = _dedash((await ask_persona(
            f"Here is a reply you just drafted for the owner:\n{reply}\n\nRewrite it so it keeps every figure "
            f"EXACTLY (never change the sort code {sort_txt}, the account {acct_txt}, or any amount) and adds a "
            f"short, natural caution about trusting the bank details. Context: {fact} {sample}",
            role="chat")).strip())
    except Exception:
        revised = ""
    # Backstop conditions: reflection must keep the exact figures AND carry a real caution cue.
    if revised and sort_txt in revised and acct_txt in revised and _CAUTION_CUE_RE.search(revised):
        return revised
    # A placeholder can never be trusted, so force the strong "do not pay" backstop even if grounded.
    safe_grounded = grounded and not (_is_placeholder_number(sort_txt) or _is_placeholder_number(acct_txt))
    return reply + _bank_details_note(reply, grounded=safe_grounded)


async def ask_persona(prompt: str, *, role: str = "chat") -> str:
    """Convenience wrapper that injects the Mongoose persona card as system."""
    return _dedash(await ask_ollama(prompt, system=_persona_card("default"), role=role))


async def _ground(prompt: str, facts: str, *, require: "list[str] | None" = None) -> "str | None":
    """Let the MODEL phrase a reply grounded in deterministic `facts` (the owner's always-think principle):
    natural, in context, using ONLY the facts. If `require` is given (e.g. exact money figures), the
    reply MUST contain every one, otherwise return None so the caller keeps the exact deterministic
    text. This protects critical numbers (RTM finances) from being reworded wrong. Never raises;
    returns None on any failure so the caller falls back to its canned text."""
    try:
        reply = (await ask_persona(
            f"The owner said: {prompt!r}\n\nReply briefly in your own voice, actually addressing what they "
            f"asked (including any follow-up or complaint). Use ONLY these facts, and never invent or alter "
            f"any number, name, or figure. You CANNOT take any action or look anything up right now, so do "
            f"NOT say you will check, fetch, re-run, or do anything. If the facts do not answer, say so "
            f"plainly:\n{facts}", role="chat")).strip()
        if not reply:
            return None
        if require and not all(str(r) in reply for r in require):
            return None
        return reply
    except Exception:
        return None


def _rule_ok(rule_id: str, prompt: str, conversation_id: str = "") -> bool:
    """Likelihood gate for every deterministic short-circuit: a rule stands down when the person
    reacted to its last firing with confusion, a verbatim repeat, or the same gist reworded, so
    the message reaches the actual model instead of the same canned card again. Fail-open."""
    try:
        from .services.rule_engagement import rule_ok
        return rule_ok(rule_id, prompt, conversation_id)
    except Exception:
        return True


# A meta-question about what Mongoose can do ("can you X", "do you support X", "how do I X"). Used as
# a LATE fallback: it only fires after every specific handler has declined, and the capability lookup
# it triggers answers positive-only, so a false trigger simply falls through to normal chat.
_CAPABILITY_QUERY_RE = re.compile(
    r"\b(can|could|do|does|are)\s+you\b"
    r"|\b(is there a way|able to)\b"
    r"|\bhow\s+(do|can|would)\s+(i|we|you)\b"
    r"|\bdo\s+you\s+(support|have|know how|handle)\b",
    re.IGNORECASE,
)


# Shared guard for stateful "the next message answers X" interceptors (body-literacy, music, forget):
# a real command intent or a "confirm <token>" must NEVER be captured as an answer to a pending
# question. This is the general fix for the class of bug where a stale capture window ate commands.
_CONFIRM_TOKEN_RE = re.compile(r"(?i)^\s*confirm\s+[a-z0-9_-]{6,}\s*$")
_COMMAND_INTENTS = frozenset({
    "rtm_plugin", "rtm_build", "rtm_operational", "rtm_knowledge", "rtm_compliance", "guest_invite",
    "password_reset", "time_alert", "sync_checkpoint", "readiness", "benchmark", "capabilities", "prepare",
    "rtm_document", "fortune", "rtm_send_receipt", "rtm_mark_paid", "task_capture", "task_list",
})

# Holds in-flight background fortune renders so the event loop cannot GC them before they post.
_FORTUNE_TASKS: set = set()


def _looks_like_command_or_confirm(prompt: str) -> bool:
    """True if the message is a recognised command intent or a confirm token (must be routed, not
    swallowed by a pending-answer capture window). Cheap and never raises."""
    p = (prompt or "").strip()
    if not p:
        return False
    if _CONFIRM_TOKEN_RE.match(p):
        return True
    try:
        from .services.intent_resolver import resolve as _resolve
        return _resolve(p)[0] in _COMMAND_INTENTS
    except Exception:
        return False


# A "which memory should I forget?" window only stays live for this long. After that a later
# "the first one" (meant for something else entirely) must not silently delete a memory.
_FORGET_WINDOW_TTL = 600.0  # seconds


def _load_forget_window(raw: str | None) -> list[dict]:
    """Parse the stored forget window and return its candidates ONLY if still fresh, else []. Accepts
    both the timestamped envelope {'at':..,'candidates':[..]} and (for a window opened by an older
    build) a bare candidate list, which is treated as fresh once so nobody's in-flight forget breaks."""
    import json as _json
    try:
        data = _json.loads((raw or "").strip() or "null")
    except Exception:
        return []
    if isinstance(data, list):  # legacy bare-list window (no timestamp): honour it this once
        return data
    if isinstance(data, dict):
        try:
            if (time.time() - float(data.get("at", 0))) <= _FORGET_WINDOW_TTL:
                return list(data.get("candidates") or [])
        except Exception:
            return []
    return []


def _prepare_topic(prompt: str) -> str:
    """Pull the subject out of 'prepare me for the trustee meeting' -> 'the trustee meeting'."""
    import re as _re
    m = _re.search(r"\b(?:prepare|prep|get me ready)\b\s*(?:me\s+)?(?:for|to|about)?\s*(.*)$",
                   prompt or "", _re.IGNORECASE)
    topic = (m.group(1).strip() if m else "").strip(" ?.!")
    return topic or "today"


async def _read_capability_speech(intent: str, prompt: str) -> str | None:
    """Run the SAME service the CLI runs for a read-only capability intent and return its text.
    Read-only and owner-only (the caller gates on not guest_mode). Never raises."""
    try:
        if intent == "brief":
            from .services.brief import generate_brief
            out = await generate_brief(post_to_bus=False)
            return out.get("brief")
        if intent == "daily_picture":
            from .services.daily_picture import daily_operating_picture, format_dop
            return format_dop(await daily_operating_picture())
        if intent == "prepare":
            from .services.prepare import prepare_card, format_prepare
            return format_prepare(await prepare_card(_prepare_topic(prompt)))
        if intent == "suggestion":
            from .services.surpriser import pick_surprise
            d = await pick_surprise(force=True, source="on_demand")
            if d.get("surfaced"):
                return (d.get("suggestion") or {}).get("text")
            return "Nothing worth surfacing right now, everything looks handled."
        if intent == "rtm_compliance":
            from .services import rtm_secretary as S
            return S.format_status(S.status_report())
    except Exception:
        return None
    return None


async def chat_with_memory(
    prompt: str,
    *,
    conversation_id: str,
    role: str = "chat",
    mode: str | None = None,
    system: str | None = None,
    guest_mode: bool = False,  # trusted_guest: no tools, no owner memory facts
    skip_user_persist: bool = False,  # caller already persisted the user message (crash-safe send)
) -> dict:
    """A memory-aware chat turn: replays recent history, injects relevant
    long-term facts, persists the exchange, and handles explicit/auto memory.

    Returns a dict with the reply plus what was remembered, so callers can
    surface it. Short-term history and long-term facts both flow through here.

    PART 1 (atomic turn lock): the ENTIRE turn is serialised per conversation id. Two messages on the
    same conversation run one after another (the second reads the first's persisted exchange); messages
    on different conversation ids (e.g. separate guest threads) still run in parallel. See _turn_lock_for
    for the lock-ordering / deadlock-freedom argument.
    """
    async with _turn_lock_for(conversation_id):
        return await _chat_turn(
            prompt,
            conversation_id=conversation_id,
            role=role,
            mode=mode,
            system=system,
            guest_mode=guest_mode,
            skip_user_persist=skip_user_persist,
        )


async def _chat_turn(
    prompt: str,
    *,
    conversation_id: str,
    role: str = "chat",
    mode: str | None = None,
    system: str | None = None,
    guest_mode: bool = False,
    skip_user_persist: bool = False,
) -> dict:
    """The body of a single chat turn. Runs under the per-conversation turn lock held by
    chat_with_memory, so it may freely read history, act, and persist without another turn on the
    same conversation interleaving."""
    from .persona import persona_card
    from .services import memory as mem

    mem.ensure_conversation(conversation_id)

    # Memory scope for this whole turn. Guest conversations ('guest:<device>') get an isolated
    # scope so a guest's facts never reach the owner brain and vice versa. Computed once here so
    # every remember/forget below: including the early "remember X" short-circuit: uses it.
    fact_scope = mem.scope_for_conversation(conversation_id)

    # --- PART 3: deterministic confirm for outward/social proposals -------------------------------
    # If the message is exactly "confirm <token>" and the token maps to a gated SOCIAL action
    # (recommend_song / message_person / whatsapp_draft), execute it here through the SAME sensitive
    # gate. This means a "yes" never depends on the weak local model re-emitting the token. Owner-only
    # (guests have no tools, so never hold such a token). wp_plugin_action tokens are deliberately left
    # to their own bridge (they run in the background), so we only claim the social tools.
    if role == "chat" and not guest_mode:
        _conf_m = re.match(r"(?i)^\s*confirm\s+([A-Za-z0-9_\-]{6,})\s*$", prompt or "")
        if _conf_m:
            try:
                from .services.sensitive_tools import peek_pending, confirm_sensitive_action_async
                _pk = peek_pending(_conf_m.group(1))
                if _pk and _pk[0] in {"recommend_song", "message_person", "whatsapp_draft"}:
                    _res = await confirm_sensitive_action_async(_conf_m.group(1))
                    reply = _confirmed_social_reply(_res)
                    (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
                    mem.add_message(conversation_id, "assistant", reply)
                    return {"reply": reply, "remembered": [], "facts_used": [],
                            "conversation_id": conversation_id, "source": "social_confirm",
                            "result": _res}
            except Exception:
                pass  # fall through to normal chat if the confirm gate is unavailable

    # --- PART 2: deterministic intent pre-router (before the weak model tool-loop) ----------------
    # A few UNAMBIGUOUS command shapes (recommend a specific song to a named person; message a named
    # person) are routed to the exact tool here, so the unreliable local planner never has to decide
    # them (it over-fires on stated preferences and under-fires on clear commands). Matched social
    # actions go through the SAME propose/confirm gate as PART 3, so a matched recommend ASKS before it
    # sends. Anything that does not cleanly match falls through untouched to the model path below.
    if role == "chat" and not guest_mode:
        try:
            from .services.chat_prerouter import deterministic_tool_route
            _routed = deterministic_tool_route(prompt)
        except Exception:
            _routed = None
        if _routed is not None:
            _tool_name, _targs = _routed
            # The pre-router only runs for the owner (guests have no tools), so the confirmed action is
            # attributed to the owner, matching run_tool_turn's _sender injection for these tools.
            if _tool_name in ("message_person", "recommend_song"):
                _targs = {**_targs, "_sender": "owner"}
            try:
                from .services.sensitive_tools import propose_sensitive_action
                _proposal = propose_sensitive_action(_tool_name, _targs)
                reply = _social_proposal_reply(_tool_name, _targs, _proposal)
                (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
                mem.add_message(conversation_id, "assistant", reply)
                return {"reply": reply, "remembered": [], "facts_used": [],
                        "conversation_id": conversation_id, "source": f"prerouter:{_tool_name}",
                        "proposal": _proposal}
            except Exception:
                pass  # fall through to the model path if proposing fails

    # --- Deterministic memory-intent layer (claim integrity) -------------------------------------
    # Runs BEFORE the model so memory behaviour is predictable. Handles: natural forget requests
    # (with real deletion, never a false "I forgot that"), selection replies to a prior ambiguous
    # forget, and casual preference statements ("really fond of Hawaiian"). Emits structured
    # memory_events so every surface can show what changed.
    from .memory_intent import detect_memory_intent, preference_to_fact
    from .db import get_app_state, set_app_state
    _forget_key = f"forget_pending:{conversation_id}"
    # A stale "which memory should I forget?" window must not turn a later "the first one" (meant for
    # something else) into a real deletion: it expires (TTL) and never reads a command as a selection.
    _forget_pending = _load_forget_window(get_app_state(_forget_key))
    if not _forget_pending and (get_app_state(_forget_key) or "").strip():
        set_app_state(_forget_key, "")  # expired/garbage window: clear it so it can't linger
    awaiting = bool(_forget_pending) and not _looks_like_command_or_confirm(prompt)
    mi = detect_memory_intent(prompt, awaiting_forget_choice=awaiting)

    if mi.intent == "forget_memory":
        import json as _json
        (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
        events: list[dict] = []
        # resolve a selection reply ("the first one" / "#2") against the pending candidates
        if mi.selection and awaiting:
            pending = _forget_pending  # already parsed + freshness-checked above
            idx = mi.selection - 1
            if 0 <= idx < len(pending):
                cand = pending[idx]
                ok = mem._delete_fact_by_id(cand["id"])
                set_app_state(_forget_key, "")
                if ok:
                    reply = f"Done, I've forgotten that. ({cand['fact']})"
                    events = [{"type": "forgotten", "fact": cand["fact"]}]
                else:
                    reply = "That memory was already gone."
            else:
                reply = "That wasn't one of the options. Could you say the number again?"
        else:
            res = mem.forget_memory(mi.target, polarity_hint=mi.polarity, scope=fact_scope)
            if res["status"] == "forgotten":
                reply = f"Done, I've forgotten that. ({res['fact']})"
                events = [{"type": "forgotten", "fact": res["fact"]}]
                set_app_state(_forget_key, "")
            elif res["status"] == "needs_confirmation":
                set_app_state(_forget_key, _json.dumps({"at": time.time(), "candidates": res["candidates"]}))
                lines = "\n".join(f"  {n+1}. {c['fact']}" for n, c in enumerate(res["candidates"]))
                reply = ("I found more than one memory that could match. Which should I forget?\n"
                         + lines + "\n(reply with the number, e.g. \"the first one\")")
                events = [{"type": "forget_needs_confirmation",
                           "candidates": res["candidates"]}]
            else:
                reply = "I couldn't find a matching memory to forget."
                events = [{"type": "memory_update_failed", "detail": "no matching memory"}]
        mem.add_message(conversation_id, "assistant", reply)
        return {"reply": reply, "remembered": [], "memory_events": events,
                "facts_used": [], "conversation_id": conversation_id}

    # a casual preference statement: capture it deterministically (don't depend on the model), but
    # STILL let the model produce a natural conversational reply below. We store now and attach the
    # event to the normal reply.
    _pref_event: dict | None = None
    if mi.intent == "remember_preference":
        fact = preference_to_fact(mi)
        stored = mem.remember_preference(fact, polarity=mi.polarity, target=mi.target, source="preference", scope=fact_scope)
        if stored.get("superseded"):
            _pref_event = {"type": "updated", "fact": fact,
                           "superseded": stored["superseded"], "confidence": mi.confidence}
        elif stored.get("stored"):
            _pref_event = {"type": "remembered", "fact": fact, "confidence": mi.confidence}

    # Explicit "remember X" short-circuits: store the fact, confirm, and do NOT
    # call the model or run model-assisted extraction (which would otherwise
    # store a near-duplicate). Only short-circuit when the message is actually a
    # remember command, not merely contains the word.
    explicit = mem.detect_explicit_memory(prompt)
    remembered: list[str] = []
    if explicit and prompt.strip().lower().startswith(("remember", "please remember")):
        mem.remember_fact(explicit, source="explicit", scope=fact_scope)
        (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
        reply = f"Got it. I'll remember that {explicit}."
        mem.add_message(conversation_id, "assistant", reply)
        return {"reply": reply, "remembered": [explicit], "facts_used": [], "conversation_id": conversation_id}
    if explicit:
        mem.remember_fact(explicit, source="explicit", scope=fact_scope)
        remembered.append(explicit)

    # Active body-literacy flow ALWAYS intercepts first: if a guided check-in / reset / journal is
    # in progress, this turn is the user's answer to it, so route to advance() before any other
    # short-circuit could mis-handle a reply like "chest" or "more settled". The flow honors "stop".
    from .services import body_literacy as BL
    # A body-literacy flow must NOT swallow a real command or a confirm token (the same class of bug
    # that let the music capture eat "build RTM" and confirm tokens all session). If the message is a
    # recognised command intent or a "confirm <token>", skip the flow entirely and let it route.
    if BL.flow_active(conversation_id) and not _looks_like_command_or_confirm(prompt):
        try:
            step = BL.advance(conversation_id, prompt)
            if not step.get("no_flow"):
                reply = step["speech"]
                (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
                mem.add_message(conversation_id, "assistant", reply)
                return {"reply": reply, "remembered": remembered, "facts_used": [],
                        "conversation_id": conversation_id, "source": "body_literacy"}
        except Exception:
            pass

    # Music onboarding lives in chat now (there is no music tab). Two things happen on every owner turn:
    # the proactive schedule is pinged so it advances/freezes with real chatting (not only tool turns),
    # and if Mongoose recently asked a music question here, a plain chatty reply is recorded as its
    # answer. We only treat it as an answer when it reads like one (chat intent, not a fresh command or
    # question), so "turn off the lights" or "what's the weather?" are never miscaptured.
    if not guest_mode:
        try:
            from .services import music_taste as _mt
            _mt.note_chat_activity(_mt.OWNER_SCOPE)
            if _mt.pending_facet(_mt.OWNER_SCOPE):
                from .role_router import classify_role as _cls
                _p = prompt.strip()
                # A pending music question must NOT swallow a message that is really a REQUEST for
                # something else ("benchmark results please", "arrears report", "show me my email").
                # Only a genuine casual reply is the answer.
                _is_request = bool(re.search(
                    r"(?i)\bplease\b|\b(results?|report|status|dashboard|summary|update|benchmark|"
                    r"arrears|email|messages?|brief|list|fetch|pull up)\b", _p))
                # And it must NOT be a real COMMAND intent (build/RTM/reset/invite/timer...). This is what
                # stops "Implement RTM_DOCUMENT_INTAKE_SPEC.md" being filed as a music-taste answer instead
                # of building the plugin. We only exclude command intents, NOT casual ones a genre word can
                # trigger (e.g. "jazz" -> home_control), so real taste answers are still captured.
                from .services.intent_resolver import resolve as _resolve_guard
                _CMD_INTENTS = {"rtm_plugin", "rtm_build", "rtm_operational", "rtm_knowledge",
                                "rtm_compliance", "guest_invite", "password_reset", "time_alert",
                                "sync_checkpoint", "readiness", "benchmark", "capabilities", "prepare",
                                "rtm_document", "fortune"}
                _is_answer = (bool(_p) and _cls(_p) == "chat" and not _p.endswith("?")
                              and not _is_request
                              and _resolve_guard(_p)[0] not in _CMD_INTENTS
                              and not re.match(r"(?i)^(what|who|whats|who's|when|where|why|how|is|are|can|could|would|do|does|did|should|will|tell me|show me|give me|remind|set|turn|play|search|find|open|check|build|rebuild|implement|make|create|deploy|run|add|generate|author|reset|invite|fix)\b", _p))
                if _is_answer:
                    facet = _mt.capture_answer(_p, _mt.OWNER_SCOPE)
                    if facet:
                        # Ground the acknowledgement in what they ACTUALLY said (always-think principle); the
                        # canned pool is only the fallback if the local model is unreachable.
                        import random as _rnd
                        _fallback = _rnd.choice([
                            "Noted. I'll remember that about your taste.",
                            "Good to know. That helps me learn what you're into.",
                            "Got it. Filed under your music taste.",
                            "Nice one. That's on file now.",
                        ])
                        try:
                            reply = (await ask_persona(
                                f"The owner just shared something about their music taste: {_p!r}. You have "
                                "filed it. Acknowledge warmly in ONE short line, in your own voice, showing you "
                                "registered the specific thing. Do not ask a question or invent extra facts.",
                                role="chat")).strip() or _fallback
                        except Exception:
                            reply = _fallback
                        (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
                        mem.add_message(conversation_id, "assistant", reply)
                        return {"reply": reply, "remembered": remembered, "facts_used": [],
                                "conversation_id": conversation_id, "source": "music_answer",
                                "music_event": {"type": "answer_recorded", "facet": facet}}
        except Exception:
            pass

    # ── Unified intent resolver: ONE decision for this turn ──────────────────────────────────────
    # Replaces the old first-match race, where each detector competed by keyword coincidence and chain
    # ORDER. Now one classifier (keyword signal + embedding similarity, single global argmax) names the
    # intent. Each handler below fires when the resolver names its intent OR , only when the resolver
    # abstains (None) , when its own keyword detector matches. So a confident resolver decision wins,
    # and anything it is unsure about falls back to exactly the previous keyword behaviour: no regression.
    _resolved = None
    try:
        from .services.intent_resolver import resolve as _resolve_intent
        _resolved = _resolve_intent(prompt, guest=guest_mode)[0]
    except Exception:
        _resolved = None

    def _fires(intent: str, keyword_hit: bool) -> bool:
        return (_resolved == intent) or (_resolved is None and bool(keyword_hit))

    # Readiness short-circuit: "are you ready?", "what needs me?", "what's broken?", "what are
    # you watching?" are answered from the READINESS layer (real evidence), not the model
    # (which would guess about its own state). Spoken-shaped: short, ranked, one action.
    from .role_router import looks_like_readiness_query
    if _fires("readiness", looks_like_readiness_query(prompt)) and not guest_mode and _rule_ok("readiness", prompt, conversation_id):
        try:
            from .services.readiness_presenter import (
                readiness_for_voice, needs_me_for_voice, what_is_broken_for_voice, watching_for_voice)
            low = prompt.strip().lower()
            if "watching" in low or "tracking" in low or "monitoring" in low:
                speech = watching_for_voice()["speech"]
            elif "broken" in low or "wrong" in low or "down" in low or "failing" in low:
                speech = what_is_broken_for_voice()["speech"]
            elif "needs" in low:
                speech = needs_me_for_voice()["speech"]
            else:
                speech = readiness_for_voice()["speech"]
            speech = await _ground(prompt, speech) or speech  # model phrases it; canned text is the fallback
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", speech)
            return {"reply": speech, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "readiness"}
        except Exception:
            pass  # fall through to normal chat if readiness can't answer

    # Example Court WordPress plugin author/build/deploy bridge. This must sit BEFORE the general
    # RTM build-status voice card: "make the plugin useful" is an action request, not a status query.
    # It proposes a sensitive action and waits for an explicit confirmation token; it never edits or
    # deploys merely because the model inferred an intent.
    if not guest_mode and (_resolved == "rtm_plugin" or _resolved is None):
        try:
            from .services.rtm_plugin_chat import maybe_handle_plugin_chat
            plugin_chat = await maybe_handle_plugin_chat(prompt, conversation_id, force=(_resolved == "rtm_plugin"))
            if plugin_chat:
                reply = plugin_chat["reply"]
                (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
                mem.add_message(conversation_id, "assistant", reply)
                return {"reply": reply, "remembered": remembered, "facts_used": [],
                        "conversation_id": conversation_id,
                        "source": plugin_chat.get("source", "wp_plugin_chat"),
                        "proposal": plugin_chat.get("proposal"),
                        "result": plugin_chat.get("result")}
        except Exception:
            pass  # fall through to normal chat if the bridge itself is unavailable

    # RTM short-circuit: two levels. "what's overdue", "who owes", "the money position", "payments
    # to review", "payables due" -> the RTM OPERATIONAL view (real ledger figures). "how's the RTM
    # build going", "what's left to build" -> the RTM BUILD view (plan progress). Both read real
    # state, never the model guessing, and both are READ-ONLY , answering never sends or pays.
    from .role_router import (looks_like_rtm_operational, looks_like_rtm_build, rtm_operational_focus)
    from .role_router import looks_like_rtm_knowledge, looks_like_sync_checkpoint, looks_like_comms_lookup
    # A voicemail / who-called lookup must NOT be swallowed by the RTM voice views: RTM_OPERATIONAL_INTENT
    # matches bare words like "leaseholder" or "example court", so "read me the voicemail from the
    # leaseholders" would otherwise land in the ledger view. When it's a comms lookup, skip the whole RTM
    # family and let it fall through to the tool loop, which searches voicemails.
    _comms_lookup = looks_like_comms_lookup(prompt)
    if _fires("sync_checkpoint", looks_like_sync_checkpoint(prompt)) and not guest_mode and not _comms_lookup and _rule_ok("sync_checkpoint", prompt, conversation_id):
        try:
            from .services.rtm_voice import sync_for_voice
            speech = sync_for_voice()["speech"]
            speech = await _ground(prompt, speech) or speech
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", speech)
            return {"reply": speech, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "sync_checkpoint"}
        except Exception:
            pass
    if _fires("rtm_knowledge", looks_like_rtm_knowledge(prompt)) and not guest_mode and not _comms_lookup and _rule_ok("rtm_knowledge", prompt, conversation_id):
        try:
            from .services.rtm_voice import knowledge_for_voice
            speech = knowledge_for_voice(prompt)["speech"]
            speech = await _ground(prompt, speech) or speech
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", speech)
            return {"reply": speech, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "rtm_knowledge"}
        except Exception:
            pass
    if _fires("rtm_build", looks_like_rtm_build(prompt)) and not guest_mode and not _comms_lookup and _rule_ok("rtm_build", prompt, conversation_id):
        try:
            from .services.rtm_voice import build_for_voice
            speech = build_for_voice()["speech"]
            speech = await _ground(prompt, speech) or speech
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", speech)
            return {"reply": speech, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "rtm_build"}
        except Exception:
            pass  # fall through to normal chat
    if _fires("rtm_operational", looks_like_rtm_operational(prompt)) and not guest_mode and not _comms_lookup and _rule_ok("rtm_operational", prompt, conversation_id):
        try:
            from .services.rtm_voice import operational_for_voice
            speech = operational_for_voice(rtm_operational_focus(prompt))["speech"]
            # FINANCIAL: ground the model to phrase it, but every exact money figure in the deterministic
            # answer MUST survive verbatim, else fall back to it. Protects RTM figures from being reworded.
            _figs = re.findall(r"£[\d,]+(?:\.\d{2})?", speech)
            speech = (await _ground(prompt, speech, require=_figs)) or speech
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", speech)
            return {"reply": speech, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "rtm_operational"}
        except Exception:
            pass  # fall through to normal chat

    # RTM chat ACTIONS + task capture/list. These intents exist BECAUSE their absence was the routing
    # disaster: with no target to route to, a task capture fell through and "...rtm task" was swallowed
    # by the arrears view. The resolver now names them deterministically (action-first), so each acts.
    if _fires("rtm_send_receipt", False) and not guest_mode and _rule_ok("rtm_send_receipt", prompt, conversation_id):
        try:
            from .services.chat_actions import send_receipt_from_chat
            reply = await send_receipt_from_chat(prompt)
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", reply)
            return {"reply": reply, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "rtm_send_receipt"}
        except Exception:
            pass
    if _fires("rtm_mark_paid", False) and not guest_mode and _rule_ok("rtm_mark_paid", prompt, conversation_id):
        try:
            from .services.chat_actions import mark_paid_from_chat
            reply = await mark_paid_from_chat(prompt)
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", reply)
            return {"reply": reply, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "rtm_mark_paid"}
        except Exception:
            pass
    if _fires("task_list", False) and not guest_mode and _rule_ok("task_list", prompt, conversation_id):
        try:
            from .services.chat_actions import list_tasks_reply
            reply = list_tasks_reply(prompt)
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", reply)
            return {"reply": reply, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "task_list"}
        except Exception:
            pass
    if _fires("task_capture", False) and not guest_mode and _rule_ok("task_capture", prompt, conversation_id):
        try:
            from .services.chat_actions import capture_task
            prior = ""
            try:
                _hist = mem.recent_messages(conversation_id, limit=6)
                _us = [m.get("content", "") for m in _hist if m.get("role") == "user"]
                prior = _us[-1] if _us else ""
            except Exception:
                prior = ""
            reply = capture_task(prompt, prior)
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", reply)
            return {"reply": reply, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "task_capture"}
        except Exception:
            pass

    # Time Alerts short-circuit: "set an alarm for 7", "cancel all timers", "what alarms do I
    # have" are handled by the time_alerts layer (deterministic parsing + ledger), not the model
    # (which can't actually schedule anything). Set/list answer directly; cancel-all asks first.
    from .role_router import looks_like_time_alert, time_alert_action
    if _fires("time_alert", looks_like_time_alert(prompt)) and not guest_mode and _rule_ok("time_alerts", prompt, conversation_id):
        try:
            from .services import time_alerts as TA
            action = time_alert_action(prompt)
            low = prompt.strip().lower()
            kind = ("timer" if "timer" in low else "reminder" if ("reminder" in low or "remind" in low)
                    else "alarm")
            if action == "list":
                alerts = TA.list_alerts()
                if not alerts:
                    reply = "You have no upcoming alarms, timers or reminders."
                else:
                    reply = "Here's what's set: " + "; ".join(_describe_alert(a) for a in alerts) + "."
            elif action == "cancel":
                # cancel-all needs confirmation; a single unambiguous cancel by description is
                # harder to resolve safely from free text, so route the user to be specific.
                if re.search(r"\ball\b|\bevery\b", low):
                    actives = TA.list_alerts(kind=kind if kind in ("alarm", "timer", "reminder") else None)
                    reply = (f"That would cancel {len(actives)} {kind}(s). To confirm, say "
                             f"'cancel all {kind}s, yes' or cancel a specific one by its time.")
                else:
                    reply = ("Tell me which one to cancel by its time (for example 'cancel the 7am "
                             "alarm'), or say 'what alarms do I have' to see them with their ids.")
            else:  # set
                # A BARE "nudge me next week" attaches its reminder to the task the owner just created,
                # instead of an orphan alarm or a confabulated "I'll nudge you" that set nothing (the
                # exact hallucination M caught). A labelled reminder ("remind me to call Bob") falls through.
                from .services.chat_actions import attach_nudge_to_recent_task
                attached = attach_nudge_to_recent_task(prompt)
                if attached:
                    reply = attached
                else:
                    r = TA.set_from_text(prompt, kind=(kind if kind != "alarm" or "alarm" in low or "wake" in low else None))
                    if not r.get("ok"):
                        # No parseable time. A "remind me to X" with no clock time is a to-do, not a dead
                        # end: keep it as an open task instead of the old "I couldn't work out a time"
                        # rejection (the second half of the chat disaster).
                        from .services.chat_actions import capture_task
                        reply = capture_task(prompt, untimed_reminder=True)
                    else:
                        reply = r["say"]
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", reply)
            return {"reply": reply, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "time_alerts"}
        except Exception:
            pass  # fall through to normal chat if anything goes wrong

    # Delight short-circuit: "tell me a joke", "fun fact", "cheer me up" get a warm, interest-aware
    # moment from the delight layer (a fact or joke tied to what Alex cares about), NOT a capability
    # tour and NOT the raw model. Checked before tutorial so the light register wins.
    from .role_router import looks_like_delight
    if _fires("delight", looks_like_delight(prompt)) and not guest_mode and _rule_ok("delight", prompt, conversation_id):
        try:
            from .services.delight import delight_for_voice
            speech = delight_for_voice(prompt)["speech"]
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", speech)
            return {"reply": speech, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "delight"}
        except Exception:
            pass  # fall through to normal chat

    # Tutorial short-circuit: "introduce yourself", "what can you do", "do something cool" are
    # answered by the tutorial layer (grounded in real readiness state), not the model. The
    # intro adapts to what's connected; "do something cool" does the coolest real thing it can,
    # or a clearly-labelled worked sample on a bare install.
    from .role_router import looks_like_tutorial, tutorial_action
    if _fires("tutorial", looks_like_tutorial(prompt)) and not guest_mode and _rule_ok("tutorial", prompt, conversation_id):
        try:
            from .services import tutorial as TUT
            if tutorial_action(prompt) == "cool":
                cool = await TUT.do_something_cool()
                reply = cool["speech"]
            else:
                reply = TUT.intro_speech()
                TUT.mark_intro_seen()
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", reply)
            return {"reply": reply, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "tutorial"}
        except Exception:
            pass  # fall through to normal chat

    # Body State short-circuit: logging movement/physio/swim/body-state, and asking about
    # load/recovery, are handled by the body-state layer (deterministic capture + the honest
    # three-mode load estimate), not the model. source:"body_state".
    from .role_router import looks_like_body_log, looks_like_body_readiness, looks_like_body_flow
    # Unstucking / active-recovery: when the user says they're stuck or grinding, the unstuck layer
    # answers (names the grind, offers one active/playful thing , never "rest"), not the model.
    from .role_router import looks_like_unstuck, unstuck_action
    if _fires("unstuck", looks_like_unstuck(prompt)) and not guest_mode and _rule_ok("unstuck", prompt, conversation_id):
        try:
            from .services import unstuck as U
            if unstuck_action(prompt) == "what_helps":
                reply = U.what_helps()["speech"]
            else:
                reply = U.unstick(force=True)["speech"]
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", reply)
            return {"reply": reply, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "unstuck"}
        except Exception:
            pass
    _body_ok = (_resolved == "body_state" or _resolved is None)
    if _body_ok and looks_like_body_flow(prompt) and not guest_mode and _rule_ok("body_flow", prompt, conversation_id):
        try:
            from .role_router import body_flow_kind
            from .services import body_literacy as BL
            spec = body_flow_kind(prompt)
            if spec["flow"] == "checkin":
                started = BL.start_checkin(conversation_id)
            elif spec["flow"] == "reset":
                started = BL.start_reset(conversation_id, kind=spec.get("kind", ""))
            else:
                started = BL.start_journal(conversation_id, kind=spec.get("kind", ""))
            reply = started["speech"]
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", reply)
            return {"reply": reply, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "body_literacy"}
        except Exception:
            pass

    if _body_ok and (looks_like_body_log(prompt) or looks_like_body_readiness(prompt)) and not guest_mode and _rule_ok("body_state", prompt, conversation_id):
        try:
            if looks_like_body_log(prompt):
                from .services.body_log_parse import log_from_text
                reply = log_from_text(prompt)["say"]
            else:
                from .role_router import body_readiness_focus
                from .services import body_voice as BV
                focus = body_readiness_focus(prompt)
                if focus == "nb:progression":
                    reply = BV.progression_answer(prompt)["speech"]
                elif focus.startswith("nb:"):
                    reply = BV.not_built_yet(focus[3:])["speech"]
                else:
                    reply = {
                        "swim_or_lift": BV.swim_or_lift,
                        "puffy": BV.puffiness_today,
                        "right_side": BV.right_side,
                        "physio": BV.need_physio,
                        "best_session": BV.best_session,
                        "today": BV.body_today,
                    }.get(focus, BV.body_today)()["speech"]
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", reply)
            return {"reply": reply, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "body_state"}
        except Exception:
            pass  # fall through to normal chat

    # Guest invite: "give me a pair/invite/access link for Sam", "invite Sam", "login details for X".
    # M says "pair link" (and by voice "repair link"); all mean the same guest-access link. Owner-only.
    # Creating an invite does NOT send anything , it produces a single-use link the owner shares , so it
    # returns the savable link directly instead of the model hallucinating about "repair links".
    from .services.intent_resolver import _guest_invite_cmd
    if _fires("guest_invite", _guest_invite_cmd(prompt)) and not guest_mode and _rule_ok("guest_invite", prompt, conversation_id):
        try:
            import re as _re
            from .guest_invites import create_guest_invite
            who = None
            for _pat in (r"\bfor\s+([A-Za-z]+)", r"\binvite\s+([A-Za-z]+)", r"\badd\s+([A-Za-z]+)"):
                _mm = _re.search(_pat, prompt or "", _re.I)
                if _mm and _mm.group(1).lower() not in {
                        "the", "a", "an", "my", "his", "her", "their", "them", "guest", "him", "someone"}:
                    who = _mm.group(1).capitalize()
                    break
            who = who or "Guest"
            res = create_guest_invite(owner_label=who, suggested_name=who, valid_days=14)
            if res.get("ok"):
                url = res.get("invite_url") or res.get("url")
                exp = (res.get("expires_at") or "")[:10]
                speech = (f"Here is {who}'s guest access link (single-use, expires {exp}). Save it and send "
                          f"it to {who}; nothing is shared until you do:\n{url}")
            else:
                probs = "; ".join(res.get("problems", []) or []) or res.get("message", "guest access is not ready")
                speech = f"I couldn't create a guest link yet: {probs}"
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", speech)
            return {"reply": speech, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "guest_invite"}
        except Exception:
            pass  # fall through to normal chat

    # Password reset as a CHAT action (owner-only): the phone has no reset UI, only chat, so "reset
    # Sam's password" must work here. The owner is invoking it, so it is owner-authorised (same as the
    # cockpit reset control). Returns the new passphrase to relay.
    from .services.intent_resolver import _reset_pw_cmd
    if _fires("password_reset", _reset_pw_cmd(prompt)) and not guest_mode and _rule_ok("password_reset", prompt, conversation_id):
        try:
            import re as _re
            from .services import auth_accounts as AA
            _m = (_re.search(r"reset\s+(?:the\s+)?password\s+for\s+([A-Za-z]+)", prompt or "", _re.I)
                  or _re.search(r"reset\s+([A-Za-z]+)'?s?\s+(?:password|login)", prompt or "", _re.I)
                  or _re.search(r"(?:new\s+password|login)\s+for\s+([A-Za-z]+)", prompt or "", _re.I))
            who = (_m.group(1) if _m else "").strip()
            if who.lower() in ("my", "the", "a", "an", "their", "his", "her"):
                who = ""
            if not who:
                speech = "Whose password should I reset? For example: 'reset Sam's password'."
            else:
                acct = AA.find_user(who)
                if not acct:
                    speech = f"I couldn't find an account called '{who}'. Check the name, or see Guests in Cockpit."
                else:
                    _r = AA.reset_password(acct["user_id"])
                    if _r.get("ok"):
                        speech = (f"Done. {_r.get('display_name') or who}'s new password is: {_r['password']}\n"
                                  f"They sign in with username '{_r.get('username')}' and that password. "
                                  f"Read it to them once; it isn't stored anywhere you can see it again.")
                    else:
                        speech = f"I couldn't reset that: {_r.get('error')}"
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", speech)
            return {"reply": speech, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "password_reset"}
        except Exception:
            pass

    # Fortune cookie card (delightful, for EVERYONE incl guests, private + public chat). Renders an A5
    # PDF via a Swift subprocess, so it runs OFF the event loop (asyncio.to_thread). Not gated on
    # guest_mode on purpose: a guest asking for a fortune should get one.
    from .services.intent_resolver import _fortune_cmd
    if _fires("fortune", _fortune_cmd(prompt)) and _rule_ok("fortune", prompt, conversation_id):
        try:
            import asyncio as _aio_f
            from .services.fortune_cookie import maybe_handle_fortune
            _cid_f, _owner_f = conversation_id, (not guest_mode)

            async def _bake_fortune() -> None:
                # Render OFF the request path so the phone never freezes: acknowledge instantly, post the
                # sealed card when the Swift render finishes (the client's history poll picks it up).
                try:
                    _f = await _aio_f.to_thread(maybe_handle_fortune, prompt, "", _owner_f)
                    mem.add_message(_cid_f, "assistant",
                                    (_f or {}).get("reply") or "The fortune cookie crumbled. Try once more?")
                except Exception:
                    mem.add_message(_cid_f, "assistant", "The fortune cookie crumbled. Try once more?")

            _tk = _aio_f.create_task(_bake_fortune())
            _FORTUNE_TASKS.add(_tk)
            _tk.add_done_callback(_FORTUNE_TASKS.discard)
            _ack = "🥠 Baking your fortune cookie, one moment..."
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", _ack)
            return {"reply": _ack, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "fortune"}
        except Exception:
            pass  # fall through to normal chat

    # RTM document generation as a CHAT action (owner-only): "generate a receipt for 3ECF9", "make
    # Morgan's welcome letter". Produces the branded document and returns a downloadable capability link
    # (or the cover-email draft text). It never sends anything.
    from .services.intent_resolver import _rtm_doc_cmd
    if _fires("rtm_document", _rtm_doc_cmd(prompt)) and not guest_mode and _rule_ok("rtm_document", prompt, conversation_id):
        try:
            import asyncio as _aio_d
            from .services.rtm_document_chat import maybe_handle_rtm_document
            # Render OFF the event loop: docxtpl + SQL are synchronous, and running them inline here
            # blocked the loop for every other request while a document was generated.
            _doc = await _aio_d.to_thread(maybe_handle_rtm_document, prompt, conversation_id)
            if _doc:
                speech = _doc["reply"]
                (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
                mem.add_message(conversation_id, "assistant", speech)
                return {"reply": speech, "remembered": remembered, "facts_used": [],
                        "conversation_id": conversation_id, "source": "rtm_document"}
        except Exception:
            pass  # fall through to normal chat

    # Capability showcase: "what can you do / what can I do with you" gets the warm feature list on
    # EVERY chat surface (IRC, voice, typed), not just the phone device path. Owner-only, deterministic.
    from .role_router import looks_like_capabilities
    if _fires("capabilities", looks_like_capabilities(prompt)) and not guest_mode and _rule_ok("capabilities", prompt, conversation_id):
        try:
            from .services.capability_catalog import owner_showcase
            speech = owner_showcase()
            (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
            mem.add_message(conversation_id, "assistant", speech)
            return {"reply": speech, "remembered": remembered, "facts_used": [],
                    "conversation_id": conversation_id, "source": "capabilities"}
        except Exception:
            pass  # fall through to normal chat

    # Read-only capabilities that used to be CLI-only (daily brief, daily operating picture, prepare-me
    # card, on-demand suggestion, RTM statutory compliance) now run in chat: each executes the SAME
    # service the CLI runs and returns its text. Owner-only, read-only, deterministic. Placed before the
    # generic capability explainer so these EXECUTE rather than just being described.
    if role == "chat" and not guest_mode:
        from .role_router import looks_like_read_capability
        _READ_CAP_INTENTS = {"brief", "daily_picture", "prepare", "suggestion", "rtm_compliance"}
        _read_cap = (_resolved if _resolved in _READ_CAP_INTENTS
                     else (looks_like_read_capability(prompt) if _resolved is None else None))
        if _read_cap and _rule_ok(_read_cap, prompt, conversation_id):
            speech = await _read_capability_speech(_read_cap, prompt)
            if speech:
                (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
                mem.add_message(conversation_id, "assistant", speech)
                return {"reply": speech, "remembered": remembered, "facts_used": [],
                        "conversation_id": conversation_id, "source": _read_cap}

    # Capability question fallback: after every specific handler above has declined, a "can you X /
    # how do I X / do you support X" question is answered from the LIVE capability registry (every
    # registered chat tool + guide workflow), so chat recognises ANY real capability, not just the
    # headline showcase areas, instead of handing the clueless model a question it cannot answer.
    # POSITIVE-ONLY: explain() returns None on a weak match, so we never wrongly deny a feature, and
    # ordinary chat still flows to the model/tool loop below.
    if role == "chat" and _CAPABILITY_QUERY_RE.search(prompt or ""):
        try:
            from .services.capability_index import explain
            # is_guest routes guests to the guest-safe capability set (never the owner tool registry).
            answer = explain(prompt, is_guest=guest_mode)
            if answer:
                (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
                mem.add_message(conversation_id, "assistant", answer)
                return {"reply": answer, "remembered": remembered, "facts_used": [],
                        "conversation_id": conversation_id, "source": "capability_explain"}
        except Exception:
            pass  # fall through to normal chat

    # Build the system prompt: persona card (for chat) plus any relevant facts.
    base_system = system if system is not None else (persona_card(mode) if role == "chat" else None)
    # Guests get the boundary appended LAST so the model never claims owner access (email, WhatsApp,
    # home, RTM...). This is the free-chat backstop behind the deterministic guest capability layer:
    # tools are already off for guests, but without this the persona still says "I connect to email",
    # so the model would over-promise. Applied whenever we have a system prompt for a guest chat turn.
    if guest_mode and role == "chat" and base_system:
        from .persona import GUEST_BOUNDARY
        base_system = f"{base_system}\n\n{GUEST_BOUNDARY}"
    history, facts = mem.build_context(conversation_id, prompt)
    # build_context already scopes fact READS by conversation id; guest_mode fact-blanking below
    # is redundant defence in depth (a guest conversation can only ever read its own scope).
    if guest_mode:
        facts = []
    system_parts = [p for p in [base_system] if p]
    if facts:
        system_parts.append("Things you know about the user:\n" + "\n".join(f"- {f}" for f in facts))
    # Give the music trait real material for the owner: the persona says Mongoose is music-obsessed,
    # but without the LEARNED taste it has nothing concrete to reference and rarely brings music up.
    if role == "chat" and not guest_mode and system is None:
        try:
            from .services import music_taste
            _ms = music_taste.profile_summary(music_taste.OWNER_SCOPE)
            if _ms:
                system_parts.append("Their music taste you've learned (weave it in naturally now and "
                                    "then, and build on it): " + _ms)
        except Exception:
            pass
    full_system = "\n\n".join(system_parts) if system_parts else None

    # Single-brain tool routing: if the request looks like it needs a tool (home control, search,
    # status, tasks, calendar...) route through run_tool_turn, then persist it to THIS conversation
    # so the exchange is in the shared memory. This is what makes IRC/voice/browser consistent , 
    # every access point uses the same brain, same memory, same tools. Without this, the cockpit
    # could turn lights on while IRC said "I don't have HA access". Now they agree.
    # The check is: classify_role says "tool", AND we're not already in a tool role (avoid recursion).
    # guest_mode = True means NO tools ever: home control, email, calendar etc are all off.
    if role == "chat" and not guest_mode:
        from .role_router import classify_role as _classify
        if _classify(prompt) == "tool":
            try:
                from .tools import run_tool_turn
                tool_out = await run_tool_turn(prompt)
                tool_reply = tool_out.get("reply") or ""
                if tool_reply:
                    _grounded = any((s.get("tool") in _DOC_CONTENT_TOOLS)
                                    for s in (tool_out.get("steps") or []))
                    tool_reply = await _bank_details_reflect(tool_reply, grounded=_grounded)
                    (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
                    mem.add_message(conversation_id, "assistant", tool_reply)
                    for fact in mem.auto_extract_facts(prompt):
                        mem.remember_fact(fact, source="auto", scope=fact_scope)
                        remembered.append(fact)
                    return {"reply": tool_reply, "remembered": remembered, "facts_used": facts,
                            "conversation_id": conversation_id, "source": "tool",
                            "tool": tool_out.get("tool"), "steps": tool_out.get("steps", [])}
            except Exception:
                pass  # fall through to normal chat if tool routing fails

    # Assemble messages: system, history, then the new prompt.
    settings = get_settings()
    selected = settings.model_for_role(role if role in VALID_ROLES else "chat")
    messages: list[dict[str, str]] = []
    if full_system:
        messages.append({"role": "system", "content": full_system})
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    reply = _dedash(await _chat_call(selected, messages, settings))
    # No tool ran on this path, so any bank details here are ungrounded by construction.
    reply = await _bank_details_reflect(reply, grounded=False)

    # Persist the exchange and extract durable facts from the user's text.
    (None if skip_user_persist else mem.add_message(conversation_id, "user", prompt))
    mem.add_message(conversation_id, "assistant", reply)
    for fact in mem.auto_extract_facts(prompt):
        mem.remember_fact(fact, source="auto", scope=fact_scope)
        remembered.append(fact)
    # Model-assisted extraction: only when the message plausibly states a
    # durable fact (cheap regex pre-filter), so ordinary turns stay fast and
    # only self-disclosing turns pay for the extra inference. Runs on the chat
    # model (already resident) to avoid a model swap. Never breaks the turn.
    if settings.memory_auto_extract and role == "chat" and mem.looks_fact_bearing(prompt):
        try:
            for fact in await mem.propose_facts_via_model(prompt, role="chat"):
                mem.remember_fact(fact, source="model", scope=fact_scope)
                remembered.append(fact)
        except Exception:
            pass

    # Build structured memory_events for the caller/UI: the deterministic preference capture (if any)
    # plus anything auto/model-extracted this turn. 'remembered' is kept for back-compat.
    memory_events: list[dict] = []
    if _pref_event:
        memory_events.append(_pref_event)
    for f in remembered:
        # avoid duplicating the preference event if extraction also produced the same fact
        if _pref_event and f == _pref_event.get("fact"):
            continue
        memory_events.append({"type": "remembered", "fact": f, "confidence": "medium"})

    return {"reply": reply, "remembered": remembered, "memory_events": memory_events,
            "facts_used": facts, "conversation_id": conversation_id}


async def _chat_call(selected: str, messages: list[dict[str, str]], settings) -> str:
    """Shared low-level call used by chat_with_memory (mirrors ask_ollama's
    request + error handling but takes a prebuilt messages list)."""
    payload = {
        "model": selected,
        "messages": messages,
        "stream": False,
        "think": settings.ollama_think,
        "keep_alive": settings.keep_alive_value,
        "options": _ollama_options(settings),
    }
    timeout_s = _ollama_timeout(settings)
    async with _inference_lock():
      async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            response = await asyncio.wait_for(
                client.post(f"{settings.ollama_base_url.rstrip('/')}/api/chat", json=payload),
                timeout=_ollama_timeout(settings),
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise LocalModelError(
                f"The local model ({selected}) returned an error. Try `mongoose doctor`.",
                detail=f"HTTP {exc.response.status_code} from {exc.request.url}",
            ) from exc
        except (asyncio.TimeoutError, httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            raise LocalModelError(
                "Can't reach the local model. Is Ollama running?", detail=str(exc)
            ) from exc
        message = data.get("message", {})
        content = (message.get("content") or "").strip()
        if not content:
            content = (message.get("thinking") or "").strip()
        return content


def _social_proposal_reply(tool: str, args: dict, proposal: dict) -> str:
    """The plain-language 'about to do X, confirm?' line for a deterministically-routed social action.
    Surfaces the confirm token so the owner (or the client) can reply 'confirm <token>' to execute."""
    token = proposal.get("confirm_token") or ""
    if tool == "recommend_song":
        who = args.get("to") or "them"
        song = args.get("song") or "that track"
        head = f"About to share {song} with {who}. Confirm?"
    elif tool == "message_person":
        who = args.get("to") or "them"
        text = (args.get("text") or "").strip()
        head = f'About to message {who}: "{text}". Confirm?'
    else:
        head = proposal.get("summary") or "About to do that. Confirm?"
    return f"{head}\nTo go ahead, reply: confirm {token}"


def _confirmed_social_reply(res: dict) -> str:
    """Phrase the result of a confirmed social action (recommend/message). Uses the executor's own
    honest delivery detail so we never over-claim (e.g. 'saved; they'll see it next time')."""
    if not (res or {}).get("ok"):
        return "I couldn't do that: " + str((res or {}).get("error") or "it failed.")
    return (res or {}).get("detail") or "Done."


def _describe_alert(a: dict) -> str:
    """A short spoken description of a time alert for the 'what's set' answer."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(a["fire_at"].replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        when = dt.astimezone().strftime("%-I:%M %p")
    except Exception:
        when = "the set time"
    kind = a.get("kind", "alert")
    label = a.get("label") or ""
    rec = f" every {a['recurrence']}" if a.get("recurrence") else ""
    if kind == "timer":
        return f"a timer at {when}"
    if kind == "reminder":
        return f"a reminder to {label} at {when}{rec}" if label else f"a reminder at {when}{rec}"
    return f"a {when}{rec} alarm" + (f" ({label})" if label else "")
