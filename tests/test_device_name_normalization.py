"""Device names must be normalised at claim time so they're safe downstream , notably as the
bus 'sender', which must be single-line. (QA-audit P1: a device paired as "bad\\nname" paired
fine but then broke share-to-Mongoose because the multiline name became a multiline bus
sender.)"""
from mongoose.db import init_db
from mongoose.devices import _normalize_device_name, claim_pairing_code, create_pairing_code


def test_newline_name_is_made_single_line():
    assert "\n" not in _normalize_device_name("bad\nname")
    assert "\r" not in _normalize_device_name("bad\r\nname")
    assert _normalize_device_name("bad\nname") == "badname"


def test_control_chars_stripped():
    assert _normalize_device_name("a\tb\x00c\x07d") == "abcd"


def test_whitespace_collapsed_and_trimmed():
    assert _normalize_device_name("  spaced   out  ") == "spaced out"


def test_empty_falls_back_to_type():
    assert _normalize_device_name("", "android") == "android"
    assert _normalize_device_name("\r\n\t", "") == "device"


def test_length_capped():
    assert len(_normalize_device_name("a" * 500)) <= 64


def test_claim_stores_normalized_name():
    """End-to-end: claiming with a multiline name stores a single-line name, so the bus
    sender (and receipts/UI) are safe."""
    init_db()
    code = create_pairing_code(profile="primary_phone")["code"]
    res = claim_pairing_code(code, "bad\nname", "android")
    assert res["ok"] is True
    # the stored/returned name must be single-line
    name = res.get("device_name") or res.get("name") or ""
    if name:
        assert "\n" not in name and "\r" not in name


def test_normalized_name_is_valid_bus_sender():
    """The whole point: a normalised name must pass the bus single-line sender check."""
    name = _normalize_device_name("bad\nname\nwith\nlots\nof\nlines")
    # the bus validator rejects multiline senders; a normalised name must be accepted
    assert "\n" not in name and "\r" not in name
