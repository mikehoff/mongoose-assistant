"""Poke system. Users can poke each other via chat ("poke mic") or via a cockpit button.
A poke sends a push notification to the recipient with 👆🪿. Users control whether they
allow pokes via their account profile. Display names are set during onboarding."""
from __future__ import annotations

from typing import Any

from ..db import connect


def ensure_table() -> None:
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pokes (
                poke_id     TEXT PRIMARY KEY,
                from_uid    TEXT NOT NULL,
                from_name   TEXT NOT NULL,
                to_uid      TEXT NOT NULL,
                to_name     TEXT NOT NULL,
                poked_at    TEXT NOT NULL
            )
        """)


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def send_poke(from_user_id: str, from_display: str,
                    to_user_id: str, to_display: str) -> dict[str, Any]:
    """Record a poke and push a notification to the recipient. 👆🪿

    REQUIREMENT (what poke is for): user A pokes user B, and *B* gets the notification on B's phone, not
    everyone. The route layer resolves a specific recipient and checks B's own `allow_poke` consent flag
    before calling here.

    DELIVERY (requirement now met): the push below passes target_user_id=to_user_id to send_to_phone,
    which delivers ONLY to B's account-bound Web Push subscriptions and never to the shared ntfy
    broadcast topic. The poke record is correct (to_uid = B) and the notification is now actually
    addressed to B. Remaining real constraint (documented, not a bug): delivery requires B to have an
    account-bound Web Push subscription (via /push/subscribe). If B has subscribed no device, the poke
    is recorded and the notification returns sent=False rather than leaking to the owner topic. This was
    previously a genuine drift, poke resolved B but delivered to the owner topic, closed by the
    overlooked-modules push-targeting fix and its regression in test_overlooked_social_push_privacy.py.
    """
    import secrets as _s
    ensure_table()

    poke_id = "poke_" + _s.token_hex(8)
    now = _now_iso()

    with connect() as con:
        con.execute(
            "INSERT INTO pokes (poke_id, from_uid, from_name, to_uid, to_name, poked_at) "
            "VALUES (?,?,?,?,?,?)",
            (poke_id, from_user_id, from_display, to_user_id, to_display, now))

    # Push notification: 👆🪿 in title, fun body
    notif_result = {"sent": False, "reason": "ntfy not attempted"}
    try:
        from .notifications import send_to_phone
        notif_result = await send_to_phone(
            title=f"👆🪿 {from_display} is poking you!",
            body="Wanna see what's new? Touch to see...",
            priority="default",
            tags=["point_up_2"],
            click="/device/launchpad#irc",  # tapping the poke lands you in the lounge
            target_user_id=to_user_id,
        )
    except Exception as exc:
        notif_result = {"sent": False, "reason": str(exc)}

    return {
        "ok": True,
        "poke_id": poke_id,
        "from": from_display,
        "to": to_display,
        "poked_at": now,
        "notification": notif_result,
    }


def poke_history(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Recent pokes sent to or from this user."""
    ensure_table()
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM pokes WHERE from_uid=? OR to_uid=? "
            "ORDER BY poked_at DESC LIMIT ?",
            (user_id, user_id, limit)).fetchall()
    return [dict(r) for r in rows]
