"""Consolidation Phase 1: the unified intent_resolver is the single classifier and carries the
benchmark intent that used to live only in semantic_router. Live routing (role_router._semantic_is,
app._semantic_intent_is) goes through the resolver, not the deprecated semantic_router."""


def test_benchmark_intent_now_lives_in_the_resolver():
    from mongoose.services.intent_resolver import INTENTS, resolve
    assert "benchmark" in INTENTS
    for m in ("how did the model benchmarks do", "show me the latest benchmark"):
        assert resolve(m)[0] == "benchmark", m


def test_core_intents_still_resolve_correctly():
    from mongoose.services.intent_resolver import resolve
    assert resolve("what can I do with you")[0] == "capabilities"
    assert resolve("is anything broken")[0] == "readiness"
    assert resolve("how are the arrears looking")[0] == "rtm_operational"
    assert resolve("are the lights on")[0] == "home_read"


def test_live_callers_route_through_the_resolver_not_semantic_router():
    # role_router._semantic_is and app._semantic_intent_is must delegate to intent_resolver
    import mongoose.role_router as rr
    import mongoose.app as A
    assert rr._semantic_is("are the lights on", "home_read") is True
    assert A._semantic_intent_is("benchmark results", "benchmark") is True
    # and their source imports the resolver, not semantic_router (docstring mentions are fine)
    import inspect
    for fn in (rr._semantic_is, A._semantic_intent_is):
        src = inspect.getsource(fn)
        assert "import semantic_router" not in src and "semantic_router import" not in src
        assert "intent_resolver import resolve" in src


def test_resolver_no_longer_imports_semantic_router():
    import inspect
    from mongoose.services import intent_resolver
    src = inspect.getsource(intent_resolver)
    assert "import semantic_router" not in src and "semantic_router import" not in src


def test_build_rtm_disambiguation():
    """Phase 2: an imperative 'build RTM' is the plugin-authoring COMMAND (the one RTM build), while
    'how's the RTM build going / ready yet' stays a STATUS query. This is the three-way split M hit."""
    from mongoose.services.intent_resolver import resolve
    for cmd in ("build RTM", "build the RTM", "rebuild the RTM", "build Wordpress RTM",
                "build the next wordpress rtm version",
                "Implement RTM_DOCUMENT_INTAKE_SPEC.md", "implement the RTM document intake spec"):
        assert resolve(cmd)[0] == "rtm_plugin", cmd
    for status in ("how's the RTM build going", "is the rtm build ready yet",
                   "what's left to build for the rtm"):
        assert resolve(status)[0] == "rtm_build", status
    # and a build verb must not steal a non-build RTM question
    assert resolve("how much does rtm owe")[0] == "rtm_operational"
    assert resolve("is rtm working")[0] != "rtm_plugin"


def test_guest_invite_intent():
    """M asks for a 'pair link' / (by voice) 'repair link' / 'login details' for a guest; chat must
    route to the guest_invite action and DO it, not hallucinate about software repairs."""
    from mongoose.services.intent_resolver import resolve
    for m in ("give me a repair link for Jordan", "pair link for jordan", "invite link for Jordan",
              "login details for Jordan", "invite Jordan", "add Jordan as a guest", "guest access link for jordan",
              "can you give a repair link for Jordan"):
        assert resolve(m)[0] == "guest_invite", m
    # must NOT hijack ordinary access/repair phrasing
    for m in ("access my emails", "repair the front door", "log in to gmail"):
        assert resolve(m)[0] != "guest_invite", m


def test_ordinary_chatter_does_not_false_route():
    from mongoose.services.intent_resolver import resolve
    for m in ("what's the weather like tomorrow", "tell me about the roman empire"):
        got = resolve(m)[0]
        assert got in (None,) or got not in ("benchmark", "capabilities", "readiness"), (m, got)
