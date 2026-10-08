"""RTM finance trap door: the service-charge money figures, where a fabricated number is worst.

fetch() computes the real summary (demanded / paid / outstanding, and arrears count) from the live demand
ledger and surfaces it as evidence with figures pre-formatted; compose() phrases from it. The contract then
requires every money figure in the reply to be one of these exact strings, so a made-up total cannot survive.
"""
from __future__ import annotations

from typing import Any

from ..base import Draft, Evidence, Source, TurnHandler, TurnRequest
from ..gateway import LlmGateway

_GUIDANCE = (
    "These are {{rtm.name}} RTM service-charge figures. Quote every money amount EXACTLY as written in the "
    "evidence (keep the pound sign and commas). Do not add, subtract, average, or invent any figure that is "
    "not present; if asked for one that is not here, name it as an unknown."
)


def _guidance() -> str:
    """The guidance with the RTM company's name filled from the site profile."""
    from ... import profile as _profile
    return _profile.render(_GUIDANCE)


class RtmFinanceHandler(TurnHandler):
    intent = "rtm"

    def __init__(self, gateway: "LlmGateway | None" = None) -> None:
        self.gateway = gateway or LlmGateway()

    async def fetch(self, req: TurnRequest, deps: Any) -> Evidence:
        try:
            from ...services import rtm_management as M
            rows = M.list_demands()
            live = [d for d in rows
                    if d.get("status") not in ("Superseded", "Cancelled") and int(d.get("amount_pence") or 0) > 0]
            demanded = sum(int(d["amount_pence"]) for d in live)
            outstanding = sum(int(d.get("outstanding_pence") or 0) for d in live)
            paid = demanded - outstanding
            unpaid = [d for d in live if int(d.get("outstanding_pence") or 0) > 0]
            data = {
                "total_demanded": M._money(demanded),
                "total_paid": M._money(paid),
                "total_outstanding": M._money(outstanding),
                "demands_live": len(live),
                "flats_in_arrears": len(unpaid),
            }
            return Evidence(sources=[Source(id="rtm:summary", kind="rtm_finance", data=data)])
        except Exception:
            return Evidence()

    async def compose(self, req: TurnRequest, evidence: Evidence, deps: Any) -> Draft:
        return await self.gateway.generate_draft(req.text, evidence, guidance=_guidance())
