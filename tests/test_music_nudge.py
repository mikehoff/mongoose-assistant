"""The 40-minute music nudge: if a person leaves a music question unanswered for ~40 min, the
scheduler job pushes them a personal 'come answer in your private chat' notification , once per
unanswered question, targeted at THAT person's account (owner or guest)."""
from __future__ import annotations

import time
import warnings

warnings.filterwarnings("ignore")


def _env(monkeypatch, tmp_path):
    for k, v in {"MONGOOSE_DB_PATH": str(tmp_path / "mn.sqlite3"),
                 "MONGOOSE_DATA_DIR": str(tmp_path / "d"), "MONGOOSE_API_TOKEN": "t"}.items():
        monkeypatch.setenv(k, v)
    from mongoose.db import init_db
    init_db()


def test_nudge_job_pushes_owner_and_guest_and_only_once(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from mongoose.services import music_taste as MT
    from mongoose.services import auth_accounts as AA

    # an owner identity + a guest account, each with an unanswered music question 41 min old
    AA.get_or_create_owner_account("owner-dev", "Alex")  # is_owner
    guest = AA.create_account("sam", "pw-123-456", display_name="Sam")
    for scope in (MT.OWNER_SCOPE, guest["user_id"]):
        MT.begin_if_new(scope)
        MT.mark_asked("teenage_years", scope)
        st = MT._load(scope)
        st["last_asked_at"] = time.time() - 2500
        MT._save(st)

    # capture pushes instead of really sending
    import mongoose.services.notifications as N
    sent = []

    async def fake(title, body="", **kw):
        sent.append(kw.get("target_user_id"))
        return {"sent": True}

    monkeypatch.setattr(N, "send_to_phone", fake)

    import asyncio
    from mongoose.services.scheduler import _job_music_nudge
    r = asyncio.run(_job_music_nudge())
    assert r["nudged"] == 2
    owner_uid = AA.owner_account()["user_id"]
    assert owner_uid in sent and guest["user_id"] in sent

    # running again does NOT re-nudge the same unanswered questions
    sent.clear()
    r2 = asyncio.run(_job_music_nudge())
    assert r2["nudged"] == 0 and sent == []
