"""Self-reflection / wiring audit. This is the guard that turns 'a feature is wired on one surface but
not another' (the guest music-onboarding breach) into a failing test instead of a guest complaint."""


def test_wiring_audit_has_no_gaps():
    from mongoose.services.introspection import audit
    a = audit()
    assert a["ok"], "wiring gaps found: " + "; ".join(g["detail"] for g in a["gaps"])


def test_guest_facing_features_have_full_surface_parity():
    from mongoose.services.introspection import chat_surface_matrix
    m = chat_surface_matrix()
    for feat, row in m.items():
        present = {s for s, v in row.items() if v}
        # a feature is either on NO surface or on ALL surfaces , never a partial wire
        assert not present or present == set(row), f"{feat} is wired unevenly across surfaces: {row}"


def test_music_onboarding_is_reachable_from_the_lounge_too():
    # the actual breach: guests who only used the public room were never interviewed
    from mongoose.services.introspection import chat_surface_matrix
    assert chat_surface_matrix()["music_onboarding"] == {"private_chat": True, "lounge": True}


def test_wiring_map_and_summary_are_populated():
    from mongoose.services.introspection import wiring_map, summary_text
    m = wiring_map()
    assert m["counts"]["routes"] > 50 and m["counts"]["intents"] > 5
    assert m["scheduler_jobs"], "expected some scheduled jobs"
    s = summary_text()
    assert "routes" in s and "intents" in s
