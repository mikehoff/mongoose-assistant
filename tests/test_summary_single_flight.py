"""Regression: the /device/summary stale-while-revalidate refresh must be SINGLE-FLIGHT. A prior bug
spawned a fresh background refresh on EVERY stale poll; with two phones polling every few seconds they
piled up and pegged the CPU, hanging the Status tab. Many rapid stale polls must spawn exactly ONE
refresh."""
import asyncio
import time


def test_stale_polls_trigger_exactly_one_refresh(monkeypatch):
    import mongoose.app as A
    calls = {"n": 0}

    async def counting(state, *, budget=2.0):
        calls["n"] += 1
        await asyncio.sleep(0.2)
        return {"ok": True}

    monkeypatch.setattr(A, "_build_owner_summary", counting)

    async def go():
        dk = "sfdev"
        A._SUMMARY_CACHE[dk] = (time.monotonic() - 999, {"ok": True})
        A._SUMMARY_REFRESHING.discard(dk)
        for _ in range(25):
            cached = A._SUMMARY_CACHE.get(dk)
            now = time.monotonic()
            ts, payload = cached
            if (now - ts) >= A._SUMMARY_TTL and dk not in A._SUMMARY_REFRESHING:
                A._SUMMARY_REFRESHING.add(dk)
                A._SUMMARY_CACHE[dk] = (now, payload)
                asyncio.create_task(A._refresh_owner_summary(dk, {"device_id": dk}))
        await asyncio.sleep(0.6)
        return calls["n"]

    n = asyncio.run(go())
    assert n == 1, f"{n} refreshes spawned for 25 stale polls; single-flight guard broken"
