from mongoose.persona import normalise_mode, persona_card, persona_payload, PERSONA_CARD


def test_modes_available():
    payload = persona_payload()
    assert set(payload["available_modes"]) == {"default", "adult", "admin", "weather"}


def test_unknown_mode_falls_back_to_default():
    assert normalise_mode("banana") == "default"
    assert normalise_mode(None) == "default"


def test_each_mode_card_differs():
    cards = {m: persona_card(m) for m in ("default", "adult", "admin", "weather")}
    assert len(set(cards.values())) == 4
    assert "Adult-health" in cards["adult"]
    assert "evidence-first" in cards["admin"]


def test_backward_compatible_constant_is_default_card():
    assert PERSONA_CARD == persona_card("default")
