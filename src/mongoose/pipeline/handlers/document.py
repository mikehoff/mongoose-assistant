"""Document trap door: invoices and email attachments, the surface where the original fabrication happened.

fetch() finds the relevant attachment, pulls its REAL extracted text, and runs the trusted-payee bank check
(reusing the already-shipped attachments + rtm_management work). compose() phrases from that evidence. So an
invoice's amount and bank details are quoted from the real file, and a payee whose details are new or CHANGED
is flagged, never invented.
"""
from __future__ import annotations

from typing import Any

from ..base import Draft, Evidence, Source, TurnHandler, TurnRequest
from ..gateway import LlmGateway

_GUIDANCE = (
    "Answer about this document using ONLY the evidence. Quote any amount, reference, sort code or account "
    "number EXACTLY as it appears. If bank_check_verdict is 'new_payee' or 'changed', tell the owner NOT to "
    "pay without confirming the details by phone on a number they already hold. Anything not in the evidence "
    "is an unknown, not a guess."
)


class DocumentHandler(TurnHandler):
    intent = "document"

    def __init__(self, gateway: "LlmGateway | None" = None) -> None:
        self.gateway = gateway or LlmGateway()

    async def fetch(self, req: TurnRequest, deps: Any) -> Evidence:
        try:
            from ...services import attachments as A, rtm_management as RM
            hits = A.search_attachments(req.text, limit=3)   # top-1 FTS is too fragile; give the model the
            sources = []                                     # top few candidates so the right doc is present
            for h in hits:
                rec = A.get_attachment_full(int(h["id"]))
                if not rec:
                    continue
                text = rec.get("content_text") or ""
                data: dict[str, Any] = {
                    "filename": rec.get("filename"),
                    "sender": rec.get("sender"),
                    "content_excerpt": text[:1200],
                }
                bd = RM.extract_bank_details(text)
                if bd.get("sort_code"):
                    data["sort_code"] = bd["sort_code"]
                if bd.get("account_number"):
                    data["account_number"] = bd["account_number"]
                bc = RM.assess_invoice_bank_details(text, rec.get("sender") or "", record_seen=False)
                if bc:
                    data["bank_check_verdict"] = bc.get("verdict")
                sources.append(Source(id=f"doc:{rec.get('id')}", kind="document", data=data))
            return Evidence(sources=sources)
        except Exception:
            return Evidence()

    async def compose(self, req: TurnRequest, evidence: Evidence, deps: Any) -> Draft:
        return await self.gateway.generate_draft(req.text, evidence, guidance=_GUIDANCE)
