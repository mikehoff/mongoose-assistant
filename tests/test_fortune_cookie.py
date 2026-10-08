"""Fortune cookie cards: routing, recipient parsing, and (when Swift is present) a real A5 PDF render
with a pdf-typed download link. A delightful feature still gets guardrails so it can't silently break."""

import pytest

from mongoose.services import fortune_cookie as F


@pytest.mark.parametrize("msg", [
    "generate fortune cookie", "make Jo a fortune", "give me a fortune",
    "generate fortune cookie for Jo", "fortune cookie please", "bake me a fortune",
])
def test_fortune_routing_fires(msg):
    from mongoose.services.intent_resolver import resolve
    assert resolve(msg)[0] == "fortune", (msg, resolve(msg)[0])


@pytest.mark.parametrize("msg,intent", [
    ("how much does rtm owe", "rtm_operational"),
    ("generate a receipt for 3ECF9", "rtm_document"),
    ("build the RTM plugin", "rtm_plugin"),
])
def test_fortune_does_not_steal_other_intents(msg, intent):
    from mongoose.services.intent_resolver import resolve
    assert resolve(msg)[0] == intent, (msg, resolve(msg)[0])


@pytest.mark.parametrize("msg,who", [
    ("make Jo a fortune", "Jo"),
    ("generate fortune cookie for Jo", "Jo"),
    ("make Susan's fortune", "Susan"),   # possessive stripped
    ("give me a fortune", ""),           # self
    ("fortune for me", ""),              # explicit self
])
def test_recipient_parse(msg, who):
    assert F._parse_recipient(msg) == who


def test_pick_fortune_shape():
    f = F.pick_fortune(seed=3)
    assert f["line"] and f["emoji"] and f["line"] in f["card_text"] and f["emoji"] in f["card_text"]
    # a different seed can give a different line; same seed is stable
    assert F.pick_fortune(seed=3)["card_text"] == f["card_text"]


def test_generate_and_download_token(tmp_path):
    if not F._swift():
        pytest.skip("swift not available")
    r = F.generate(recipient="Jo", seed=7)
    assert r["ok"], r
    assert r["filename"] == "Fortune_Jo.pdf" and r["download_url"].startswith("/rtm/doc/")
    from mongoose.services.rtm_templates import resolve_download
    info = resolve_download(r["download_token"])
    assert info and info["media_type"] == "application/pdf"
    # the file is a real, non-trivial PDF (colour emoji embedded makes it sizeable)
    with open(info["path"], "rb") as fh:
        head = fh.read(5)
    assert head[:4] == b"%PDF"
    assert info["path"].endswith(".pdf")


def test_chat_handler_reply_has_a_link(tmp_path):
    if not F._swift():
        pytest.skip("swift not available")
    out = F.maybe_handle_fortune("make Jo a fortune", sender_name="Sam", is_owner=False)
    assert out and out["source"] == "fortune"
    assert "Jo" in out["reply"] and "/rtm/doc/" in out["reply"] and "🥠" in out["reply"]
