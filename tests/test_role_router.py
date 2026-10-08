from mongoose.role_router import classify_role


def test_plain_chat_is_chat():
    assert classify_role("what's the weather like today?") == "chat"
    assert classify_role("tell me something funny") == "chat"
    assert classify_role("") == "chat"
    assert classify_role("   ") == "chat"


def test_home_and_tool_intents_route_to_tool():
    assert classify_role("turn off the bathroom lights") == "tool"
    assert classify_role("set a timer for 10 minutes") == "tool"
    assert classify_role("am I free on Thursday afternoon?") == "tool"
    assert classify_role("search my email for the battery order") == "tool"
    assert classify_role("remind me to call mum") == "tool"


def test_code_intents_route_to_code():
    assert classify_role("write a python function to parse this csv") == "code"
    assert classify_role("```\ndef foo():\n    return 1\n```") == "code"
    assert classify_role("fix this SQL query: SELECT * FROM users") == "code"


def test_code_beats_tool_when_both_present():
    # A coding request that also mentions scheduling should still be code.
    text = "write a python function that reads my calendar and schedules events"
    assert classify_role(text) == "code"


def test_casual_mention_of_function_is_not_code():
    # "function" in ordinary speech, no coding verb -> chat, not code.
    assert classify_role("what is the function of the spleen?") == "chat"


def test_prose_brackets_do_not_route_to_code():
    # Regression: "(music)" and "{test}" in prose must not be classified as code,
    # which previously routed them to an un-pulled code model and 404'd.
    assert classify_role("say my name say my name (music)") == "chat"
    assert classify_role("hello {test} world") == "chat"
    assert classify_role("tell me a joke (a good one)") == "chat"
    # But real code structure still routes to code.
    assert classify_role("fix this: def foo(): return 1") == "code"


def test_voicemail_lookups_route_to_tool():
    # Voicemail / who-called lookups must reach the tool loop (which searches voicemails), even when
    # they mention RTM-ish words that RTM_OPERATIONAL_INTENT matches on.
    from mongoose.role_router import classify_role
    assert classify_role("any new voicemails about example court?") == "tool"
    assert classify_role("read me the voicemail from the leaseholders") == "tool"
    assert classify_role("did i get a voicemail about the service charge?") == "tool"
    assert classify_role("play the latest voicemail") == "tool"
    assert classify_role("who called today?") == "tool"
    assert classify_role("any missed calls?") == "tool"


def test_comms_lookup_suppresses_rtm_but_a_call_command_does_not():
    # A voicemail lookup should flip the comms flag AND stop the RTM operational short-circuit; a
    # genuine RTM money question must still be RTM; placing a call is not a comms lookup.
    from mongoose.role_router import looks_like_comms_lookup, looks_like_rtm_operational
    assert looks_like_comms_lookup("read me the voicemail from the leaseholders") is True
    # RTM short-circuit only fires when it's NOT a comms lookup:
    assert (looks_like_rtm_operational("read me the voicemail from the leaseholders")
            and not looks_like_comms_lookup("read me the voicemail from the leaseholders")) is False
    # genuine RTM operational query is unaffected
    assert looks_like_comms_lookup("what's the RTM arrears position") is False
    assert looks_like_rtm_operational("what's the RTM arrears position") is True
    # a command to place a call is not a voicemail lookup
    assert looks_like_comms_lookup("call Alex") is False
