"""Tests for executable-authority claim consistency , the checker that hunts the recurring bug class:
two subsystems making the same claim with different logic."""
from mongoose.services import claim_consistency as cc


def test_agreement_when_probes_match():
    c = cc.Claim("demo", "x", probes=(
        cc.Probe("a", lambda _i: True), cc.Probe("b", lambda _i: True)))
    r = cc.check(c)
    assert r["agree"] is True and r["comparable"] is True and "divergence" not in r


def test_divergence_is_detected_and_attributed():
    c = cc.Claim("demo", "x", probes=(
        cc.Probe("trusted", lambda _i: True), cc.Probe("drifted", lambda _i: False)))
    r = cc.check(c)
    assert r["agree"] is False
    flat = {a for auths in r["divergence"].values() for a in auths}
    assert flat == {"trusted", "drifted"}   # names WHO disagrees


def test_skip_is_not_disagreement():
    c = cc.Claim("demo", "x", probes=(
        cc.Probe("a", lambda _i: True), cc.Probe("absent", lambda _i: cc.SKIP)))
    r = cc.check(c)
    assert r["agree"] is True and r["evaluated"] == 1 and r["comparable"] is False


def test_normalisation_compares_meaning_not_representation():
    c = cc.Claim("demo", "x", probes=(
        cc.Probe("a", lambda _i: "Signed_In"), cc.Probe("b", lambda _i: " signed_in ")))
    assert cc.check(c)["agree"] is True


def test_a_probe_error_is_isolated_not_fatal():
    def boom(_i): raise RuntimeError("kaboom")
    c = cc.Claim("demo", "x", probes=(cc.Probe("ok", lambda _i: 1), cc.Probe("bad", boom)))
    r = cc.check(c)   # must not raise
    assert r["results"]["bad"].startswith("ERROR:") and r["evaluated"] == 1


def test_builtin_process_identity_agrees_today():
    """After this session's fix, the installer rule and `mongoose stop`'s shared definition classify the
    sample command lines identically (incl. the 'mongoose-alpha 7' space path and the mongoose-ntfy trap)."""
    claims = {c.key: c for c in cc.claims()}
    r = cc.check(claims["process_identity"])
    assert r["comparable"] is True and r["agree"] is True, r.get("divergence")


def test_catches_the_overstrict_installer_rule():
    """THE acceptance test. Feed the over-strict rule actually shipped this session (required an exact
    invocation token AND the current install's repo path). Against an orphan from a DIFFERENT folder it
    disagrees with the trusted rule , the exact bug that cost three rounds. The checker must flag it and
    name both authorities."""
    from mongoose.cli import _command_looks_like_mongoose_server as canonical
    samples = cc._PROC_SAMPLES
    repo = "/Users/x/Downloads/mongoose-alpha 4"   # installing from a DIFFERENT folder than the orphan

    def overstrict(cmd: str) -> bool:
        has_token = ("-m mongoose serve" in cmd) or ("/bin/mongoose serve" in cmd) or ("/mongoose serve" in cmd)
        return has_token and (repo in cmd)

    c = cc.Claim("process_identity", "process identity must be one definition",
                 fixed_input=samples, probes=(
                     cc.Probe("mongoose stop (trusted)", lambda s: tuple(bool(canonical(x)) for x in s)),
                     cc.Probe("installer over-strict rule", lambda s: tuple(bool(overstrict(x)) for x in s)),
                 ))
    r = cc.check(c)
    assert r["agree"] is False, "the over-strict rule must be caught"
    # and the finding is actionable: it names both disagreeing authorities
    flat = {a for auths in r["divergence"].values() for a in auths}
    assert flat == {"mongoose stop (trusted)", "installer over-strict rule"}


def test_check_all_is_clean_now():
    r = cc.check_all()
    assert r["ok"] is True, r["diverging"]
    assert r["diverging_count"] == 0
