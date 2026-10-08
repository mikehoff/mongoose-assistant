"""MCP email-attachment retrieval + financial anti-fabrication guard.

Regression for a real, near-money incident: a leaseholder asked Mongoose (via the Claude MCP connector) for
an invoice's bank details. unified_search FOUND the invoice PDF attachment but returned display text only,
with no retrievable handle, and retrieve_file only reached uploaded documents, not email attachments. Asked
to "open" it anyway, the local chat model HALLUCINATED sort code 00-00-00 / account 12345678 (both textbook
placeholders, neither in the real PDF). The real invoice was sitting in the DB, fully extracted.

These tests lock: (1) attachment hits carry a retrieve_attachment handle, (2) retrieve_attachment returns the
REAL extracted text + bytes, (3) the bank-details caveat fires on any reply asserting bank details.
"""
import base64
import json

from mongoose.services import mcp_server as M
from mongoose.services import attachments as A


def _seed_invoice() -> int:
    payload = base64.b64encode(b"%PDF-1.4 fake invoice bytes").decode("ascii")
    res = A.index_attachment(
        source="email_attachment",
        filename="Invoice INV-9999.pdf",
        source_message_id="msg-test-1",
        mime_type="application/pdf",
        sender="Supplier <billing@example.com>",
        subject="Your invoice",
        email_date="Fri, 10 Jul 2026 09:58:25 +0000",
        content_text=("TAX INVOICE INV-9999. Amount Due GBP 580.52. "
                      "Business Bank: Barclays. Account Number: 41927536. Sort Code: 01-02-03."),
        payload_b64=payload,
    )
    assert res.get("ok"), res
    return int(res["id"])


def test_unified_search_gives_attachment_a_retrieve_handle():
    aid = _seed_invoice()
    out = M._call_tool("unified_search", {"query": "INV-9999 invoice", "limit": 5})
    assert "INV-9999" in out
    # the whole point: the caller is told exactly how to pull the real bytes
    assert f"retrieve_attachment(id={aid})" in out


def test_retrieve_attachment_returns_real_content_and_bytes():
    aid = _seed_invoice()
    res = json.loads(M._call_tool("retrieve_attachment", {"id": aid, "include_bytes": True}))
    assert res["attachment_id"] == aid
    assert res["content_indexed"] is True
    # the REAL figures, straight from the stored document, not a guess
    for real in ("01-02-03", "41927536", "580.52", "Barclays"):
        assert real in res["extracted_text"], real
    assert res.get("content_base64")  # bytes round-trip for the connector to read the actual PDF


def test_retrieve_attachment_unknown_id_is_clean():
    assert "No attachment with id" in M._call_tool("retrieve_attachment", {"id": 987654321})
    assert "numeric attachment id" in M._call_tool("retrieve_attachment", {"id": "not-a-number"})


def test_read_document_tool_returns_real_content_by_id_and_filename():
    import asyncio
    from mongoose.tools import _tool_read_document
    aid = _seed_invoice()
    by_id = asyncio.run(_tool_read_document({"attachment_id": aid}))
    assert by_id["ok"] and by_id["kind"] == "attachment"
    for real in ("01-02-03", "41927536", "580.52"):
        assert real in by_id["content"], real
    by_name = asyncio.run(_tool_read_document({"filename": "INV-9999"}))
    assert by_name["ok"] and "41927536" in by_name["content"]
    missing = asyncio.run(_tool_read_document({"attachment_id": 987654321}))
    assert missing["ok"] is False


def test_read_document_flags_missing_content_instead_of_letting_model_invent():
    import asyncio
    from mongoose.tools import _tool_read_document
    from mongoose.services import attachments as A
    res = A.index_attachment(source="email_attachment", filename="Scan No Text.pdf",
                             source_message_id="msg-empty-1", mime_type="application/pdf",
                             content_text=None)  # no extracted text (e.g. an image-only scan)
    out = asyncio.run(_tool_read_document({"attachment_id": int(res["id"])}))
    assert out["ok"] and out["content"] == ""
    assert "note" in out and "invent" in out["note"].lower()


def test_financial_anti_fabrication_net_lives_in_llm_and_is_grounded_aware():
    # The single source of truth is llm._bank_details_note (covers connector + phone + Cockpit via
    # chat_with_memory). Ungrounded bank details get a strong "do not pay" warning; grounded ones get a
    # lighter phone-confirm reminder; non-bank replies get nothing.
    from mongoose import llm
    fabricated = "Bank details: Sort Code: 00-00-00, Account Number: 12345678."
    strong = llm._bank_details_note(fabricated, grounded=False)
    assert strong and "do not pay" in strong.lower()
    light = llm._bank_details_note(fabricated, grounded=True)
    assert light and "confirm" in light.lower() and "do not pay" not in light.lower()
    assert llm._bank_details_note("The total is 580.52, due 9 Aug.", grounded=False) == ""  # no bank details
    assert llm._bank_details_note("Call me on 020 7946 0000.", grounded=False) == ""  # phone != bank details


def test_placeholder_bank_numbers_are_detected():
    from mongoose import llm
    for placeholder in ("00-00-00", "12-34-56", "11111111", "12345678", "87654321"):
        assert llm._is_placeholder_number(placeholder), placeholder
    for real in ("01-02-03", "41927536", "608371"):
        assert not llm._is_placeholder_number(real), real
    assert llm._implausible_bank_details("Sort 00-00-00 Account Number 12345678")
    assert not llm._implausible_bank_details("Sort 01-02-03 Account Number 41927536")


def test_lounge_line_is_model_first_with_template_fallback():
    import asyncio
    from mongoose.services.lounge_surpriser import compose_lounge_surprise, _model_lounge_line
    snap = {"safe_to_reflect": True, "humans": [{"id": "g1"}], "visible_names": ["Sam"],
            "topic": "the music thread", "profiles": [{"name": "Sam", "fun_fact": "likes vinyl"}],
            "next_prompt": "one skip-free song each?"}
    # unsafe snapshot -> silence
    assert compose_lounge_surprise({"safe_to_reflect": False}) is None
    # a safe snapshot always yields a line (model line if reachable, else the deterministic template)
    line = compose_lounge_surprise(snap)
    assert isinstance(line, str) and line.strip()

    # inside a running event loop, the model path stands down (no nested asyncio.run) and the template wins
    async def _inside_loop():
        return _model_lounge_line(snap)
    assert asyncio.run(_inside_loop()) is None


def test_bank_details_reflect_preserves_figures_and_always_carries_a_caution():
    # Grounded-reflection version: the MODEL phrases its own caution (no canned line) when it read the doc;
    # if reflection is unavailable or unsafe, the deterministic backstop fires. Either way the SAFETY
    # INVARIANT must hold: the exact figures survive AND some verification caution is present. No bank
    # details -> the reply passes through untouched with no extra model call.
    import asyncio
    from mongoose import llm
    passthrough = "Total is 580.52, due 9 Aug."
    assert asyncio.run(llm._bank_details_reflect(passthrough, grounded=False)) == passthrough
    for grounded in (True, False):
        src = "Pay Barclays, Sort Code 01-02-03, Account Number 41927536, total 580.52."
        out = asyncio.run(llm._bank_details_reflect(src, grounded=grounded))
        assert "01-02-03" in out and "41927536" in out  # figures never lost or altered
        assert llm._CAUTION_CUE_RE.search(out)  # a verification caution is always present
