"""The natural-language poke parser must fire on real poke commands but NOT on sentences that
only TALK ABOUT poking , explanations, examples, quotes , so 'try typing poke Alex' or 'if you
type poke Alex it will work' never actually pokes Alex."""


def _p(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "p.sqlite3"))
    import mongoose.app as A
    return A._parse_poke_target


def test_real_poke_commands_fire(monkeypatch, tmp_path):
    p = _p(monkeypatch, tmp_path)
    assert p("poke Alex") == "Alex"
    assert p("mongoose, poke Alex") == "Alex"
    assert p("poking Alex!") == "Alex"
    assert p("lets poke Alex mongoose?") == "Alex"
    assert p("poke @jordan") == "jordan"
    assert p("oi poke Casey please") == "Casey"
    assert p("poke Alex Smith") == "Alex Smith"


def test_talking_about_poking_does_not_fire(monkeypatch, tmp_path):
    p = _p(monkeypatch, tmp_path)
    for t in [
        "try typing poke Alex",
        "if you type poke Alex it will work",
        "you can poke Alex",
        "type 'poke Alex'",
        "how do I poke Alex?",
        "just say poke Casey and it works",
        "for example poke Jordan",
        "I poke fun at things",          # idiom (3+ word target)
        "poke around the settings",      # idiom
    ]:
        assert p(t) is None, t
