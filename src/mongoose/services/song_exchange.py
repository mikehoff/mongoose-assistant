"""Weekly song exchange. Once a week Mongoose asks one user to recommend a song to another.
When they do, the recipient is notified. 1-2 days later, Mongoose asks the recipient for
feedback. When they give it, the recommender is notified. One gentle reminder per step;
no harassment. All social, all opt-in (respects allow_poke preference as the social proxy,
if you're open to pokes you're open to song nudges).

REQUIREMENT MET (was a drift, now closed): this module is multi-user, it stores asker_id/recipient_id
and addresses each recipient by name. Notifications previously went through send_to_phone with no target
and reached the owner topic, not the named recipient (the same drift as poke). The overlooked-modules
fix made every _notify_* helper pass target_user_id (the asker or recipient as appropriate), so personal
song-exchange notifications now deliver only to that user's account-bound Web Push subscriptions and
never to the shared ntfy broadcast. Separately, record_recommendation/record_feedback are now
actor-bound: they reject a caller who is not the asker (for recommend) or the recipient (for feedback),
closing an authorization hole where any authenticated caller who knew an exchange id could act on
someone else's exchange. Remaining real constraint: per-user delivery requires the user to have an
account-bound Web Push subscription; with none, the notification is recorded-but-not-delivered rather
than broadcast. Regressions in test_overlooked_social_push_privacy.py."""
from __future__ import annotations

import random
import secrets
from typing import Any, Optional

from ..db import connect


def ensure_table() -> None:
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS song_exchanges (
                exchange_id          TEXT PRIMARY KEY,
                asker_id             TEXT NOT NULL,
                asker_name           TEXT NOT NULL,
                recipient_id         TEXT NOT NULL,
                recipient_name       TEXT NOT NULL,
                asked_at             TEXT NOT NULL,
                song                 TEXT,
                recommended_at       TEXT,
                feedback             TEXT,
                feedback_given_at    TEXT,
                feedback_requested_at TEXT,
                ask_reminder_sent    INTEGER NOT NULL DEFAULT 0,
                feedback_reminder_sent INTEGER NOT NULL DEFAULT 0,
                status               TEXT NOT NULL DEFAULT 'pending'
            )
        """)


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hours_since(iso: Optional[str]) -> float:
    if not iso:
        return 9999.0
    import datetime
    try:
        t = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        return (now - t).total_seconds() / 3600
    except Exception:
        return 9999.0


def pick_pair() -> Optional[tuple[dict, dict]]:
    """Pick two random users who both allow social interactions. Returns (asker, recipient) or None."""
    from .auth_accounts import list_users
    candidates = [u for u in list_users() if u.get("allow_poke")]
    if len(candidates) < 2:
        return None
    pair = random.sample(candidates, 2)
    return pair[0], pair[1]


async def create_exchange() -> Optional[dict[str, Any]]:
    """Create a new weekly song exchange and push the ask notification to the asker."""
    ensure_table()
    pair = pick_pair()
    if not pair:
        return None
    asker, recipient = pair
    exchange_id = "sx_" + secrets.token_hex(8)
    now = _now_iso()
    with connect() as con:
        con.execute(
            "INSERT INTO song_exchanges "
            "(exchange_id, asker_id, asker_name, recipient_id, recipient_name, asked_at) "
            "VALUES (?,?,?,?,?,?)",
            (exchange_id, asker["user_id"], asker["display_name"],
             recipient["user_id"], recipient["display_name"], now))
    await _notify_ask(exchange_id, asker["user_id"], asker["display_name"], recipient["display_name"])
    return {"exchange_id": exchange_id, "asker": asker["display_name"],
            "recipient": recipient["display_name"]}


async def _notify_ask(exchange_id: str, asker_id: str, asker_name: str, recipient_name: str) -> None:
    try:
        from .notifications import send_to_phone
        await send_to_phone(
            title=f"🎵 Music exchange for {asker_name}",
            body=f"Can you recommend a song to {recipient_name}? "
                 f"Reply 'recommend [song] for {recipient_name}' in Mongoose chat.",
            priority="low",
            tags=["musical_note"],
            target_user_id=asker_id,
        )
    except Exception:
        pass


async def record_recommendation(exchange_id: str, song: str, *, actor_user_id: str | None = None) -> dict[str, Any]:
    """Record a song recommendation and notify the recipient."""
    ensure_table()
    song = (song or "").strip()[:200]
    if not song:
        return {"ok": False, "error": "song name required"}
    now = _now_iso()
    with connect() as con:
        row = con.execute("SELECT * FROM song_exchanges WHERE exchange_id=?",
                          (exchange_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "exchange not found"}
        if row["status"] != "pending":
            return {"ok": False, "error": "already recommended"}
        if actor_user_id and row["asker_id"] != actor_user_id:
            return {"ok": False, "error": "only the person asked can recommend for this exchange"}
        con.execute(
            "UPDATE song_exchanges SET song=?, recommended_at=?, status='recommended' WHERE exchange_id=?",
            (song, now, exchange_id))
    await _notify_recommended(exchange_id, dict(row), song)
    return {"ok": True, "exchange_id": exchange_id, "song": song,
            "to": row["recipient_name"]}


async def _notify_recommended(exchange_id: str, row: dict, song: str) -> None:
    try:
        from .notifications import send_to_phone
        await send_to_phone(
            title=f"🎵 {row['asker_name']} recommended a song to {row['recipient_name']}",
            body=f"\"{song}\": give it a listen! Mongoose will check back in a couple of days.",
            priority="default",
            tags=["musical_note"],
            target_user_id=row.get("recipient_id"),
        )
    except Exception:
        pass


async def request_feedback(exchange_id: str) -> dict[str, Any]:
    """Ask the recipient for feedback on the recommended song (called 1-2 days after recommendation)."""
    ensure_table()
    now = _now_iso()
    with connect() as con:
        row = con.execute("SELECT * FROM song_exchanges WHERE exchange_id=?",
                          (exchange_id,)).fetchone()
        if not row or row["status"] != "recommended":
            return {"ok": False}
        con.execute(
            "UPDATE song_exchanges SET feedback_requested_at=? WHERE exchange_id=?",
            (now, exchange_id))
    await _notify_feedback_request(exchange_id, dict(row))
    return {"ok": True}


async def _notify_feedback_request(exchange_id: str, row: dict) -> None:
    try:
        from .notifications import send_to_phone
        await send_to_phone(
            title=f"🎵 What did you think of {row['asker_name']}'s pick?",
            body=f"Did you listen to \"{row['song']}\"? "
                 f"Tell Mongoose: 'feedback {exchange_id}: [your thoughts]'",
            priority="low",
            tags=["musical_note"],
            target_user_id=row.get("recipient_id"),
        )
    except Exception:
        pass


async def record_feedback(exchange_id: str, feedback: str, *, actor_user_id: str | None = None) -> dict[str, Any]:
    """Record feedback and notify the person who made the recommendation."""
    ensure_table()
    feedback = (feedback or "").strip()[:500]
    if not feedback:
        return {"ok": False, "error": "feedback text required"}
    now = _now_iso()
    with connect() as con:
        row = con.execute("SELECT * FROM song_exchanges WHERE exchange_id=?",
                          (exchange_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "exchange not found"}
        if row["status"] == "feedback_given":
            return {"ok": False, "error": "feedback already given"}
        if actor_user_id and row["recipient_id"] != actor_user_id:
            return {"ok": False, "error": "only the recipient can give feedback for this exchange"}
        con.execute(
            "UPDATE song_exchanges SET feedback=?, feedback_given_at=?, status='feedback_given' "
            "WHERE exchange_id=?",
            (feedback, now, exchange_id))
    await _notify_feedback_given(dict(row), feedback)
    return {"ok": True, "exchange_id": exchange_id, "feedback": feedback}


async def _notify_feedback_given(row: dict, feedback: str) -> None:
    try:
        from .notifications import send_to_phone
        await send_to_phone(
            title=f"💬 {row['recipient_name']} listened to your pick!",
            body=f"Their thoughts on \"{row['song']}\": {feedback}",
            priority="default",
            tags=["speech_balloon", "musical_note"],
            target_user_id=row.get("asker_id"),
        )
    except Exception:
        pass


async def run_reminder_pass() -> dict[str, Any]:
    """Daily check: send one gentle reminder if pending, request feedback at 24-48h.
    No harassment: one reminder per stage, then silence."""
    ensure_table()
    actions = []
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM song_exchanges WHERE status IN ('pending','recommended')").fetchall()

    for row in rows:
        row = dict(row)

        # Pending too long (>72h): one gentle reminder to the asker, then leave it
        if row["status"] == "pending" and not row["ask_reminder_sent"]:
            if _hours_since(row["asked_at"]) >= 72:
                try:
                    from .notifications import send_to_phone
                    await send_to_phone(
                        title=f"🎵 Still thinking about a song for {row['recipient_name']}?",
                        body="No pressure: whenever you're ready. "
                             f"Reply 'recommend [song] for {row['recipient_name']}' in Mongoose.",
                        priority="min",
                        tags=["musical_note"],
                        target_user_id=row.get("asker_id"),
                    )
                    with connect() as con:
                        con.execute(
                            "UPDATE song_exchanges SET ask_reminder_sent=1 WHERE exchange_id=?",
                            (row["exchange_id"],))
                    actions.append({"action": "ask_reminder", "id": row["exchange_id"]})
                except Exception:
                    pass

        # Recommended 24-48h ago: time to ask for feedback
        if row["status"] == "recommended" and not row["feedback_requested_at"]:
            hrs = _hours_since(row["recommended_at"])
            if 24 <= hrs <= 72:
                result = await request_feedback(row["exchange_id"])
                if result.get("ok"):
                    actions.append({"action": "feedback_requested", "id": row["exchange_id"]})

        # Feedback requested >72h ago, no response: one gentle reminder
        if (row["status"] == "recommended" and row["feedback_requested_at"]
                and not row["feedback_reminder_sent"]):
            if _hours_since(row["feedback_requested_at"]) >= 72:
                try:
                    from .notifications import send_to_phone
                    await send_to_phone(
                        title="🎵 Did you get a chance to listen?",
                        body=f"\"{row['song']}\" from {row['asker_name']}: "
                             f"even one word of feedback makes their day. No pressure though.",
                        priority="min",
                        tags=["musical_note"],
                        target_user_id=row.get("recipient_id"),
                    )
                    with connect() as con:
                        con.execute(
                            "UPDATE song_exchanges SET feedback_reminder_sent=1 WHERE exchange_id=?",
                            (row["exchange_id"],))
                    actions.append({"action": "feedback_reminder", "id": row["exchange_id"]})
                except Exception:
                    pass

    return {"ok": True, "actions": actions}


def list_exchanges(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Recent exchanges involving this user."""
    ensure_table()
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM song_exchanges WHERE asker_id=? OR recipient_id=? "
            "ORDER BY asked_at DESC LIMIT ?",
            (user_id, user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def ensure_direct_table() -> None:
    """The store for DIRECT, natural-language recommendations: one person telling Mongoose to pass a
    song to another ('tell Jordan to listen to X'). Separate from the weekly song_exchanges pairing so
    an ad-hoc recommendation is recorded durably and shows in history, never silently vanishing."""
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS direct_song_recs (
                rec_id       TEXT PRIMARY KEY,
                from_user    TEXT NOT NULL,
                from_name    TEXT NOT NULL,
                to_scope     TEXT NOT NULL,
                to_name      TEXT NOT NULL,
                song         TEXT NOT NULL,
                note         TEXT,
                created_at   TEXT NOT NULL
            )
        """)


def record_direct_recommendation(from_user: str, from_name: str, to_scope: str,
                                 to_name: str, song: str, note: str = "") -> dict[str, Any]:
    """Persist one person-to-person song recommendation so it survives in history. Lightweight: this
    only stores the row; the caller delivers the message into the recipient's chat + a push. Returns
    {ok, rec_id, song, to} or {ok: False, error}."""
    ensure_direct_table()
    song = (song or "").strip()[:200]
    if not song:
        return {"ok": False, "error": "song name required"}
    rec_id = "dsr_" + secrets.token_hex(8)
    now = _now_iso()
    try:
        with connect() as con:
            con.execute(
                "INSERT INTO direct_song_recs "
                "(rec_id, from_user, from_name, to_scope, to_name, song, note, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (rec_id, str(from_user or ""), str(from_name or "someone"), str(to_scope or ""),
                 str(to_name or ""), song, (note or "").strip()[:500], now))
    except Exception as e:
        return {"ok": False, "error": f"could not record recommendation: {e}"}
    return {"ok": True, "rec_id": rec_id, "song": song, "to": to_name, "at": now}


def list_direct_recommendations(scope: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """Recent direct recommendations sent TO or FROM a scope (both sides), newest first. Omit scope
    for all. Used for history / verification."""
    ensure_direct_table()
    with connect() as con:
        if scope:
            rows = con.execute(
                "SELECT * FROM direct_song_recs WHERE to_scope=? OR from_user=? "
                "ORDER BY created_at DESC LIMIT ?", (scope, scope, limit)).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM direct_song_recs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def find_pending_exchange(asker_name: str = "", recipient_name: str = "") -> Optional[dict]:
    """Find a pending exchange where this user is the asker (for chat command resolution)."""
    ensure_table()
    with connect() as con:
        row = con.execute(
            "SELECT * FROM song_exchanges WHERE status='pending' "
            "AND (lower(asker_name)=? OR lower(recipient_name)=?) "
            "ORDER BY asked_at DESC LIMIT 1",
            (asker_name.lower(), recipient_name.lower())).fetchone()
    return dict(row) if row else None
