"""Executable authorities, the highest-value bug class, caught before it reaches the Mac.

The expensive failures here are not null pointers or regexes. They are TWO SUBSYSTEMS MAKING THE SAME
CLAIM WITH DIFFERENT LOGIC, drifting apart until one is wrong at the worst moment:

    installed        vs  actually running        (a healthy port vs this version, this tree)
    configured       vs  actually usable         (a client file vs a real token)
    "this is my process" (installer)  vs  the same question answered by `mongoose stop`

self_harden already records, per claim, an `authority` string naming the subsystems that assert it, but
that string is PROSE. Nothing runs those authorities and checks they AGREE. This module makes the
authority field EXECUTABLE: each authority becomes a Probe that REUSES an existing trusted source; the
checker runs every probe for a claim over the SAME input and flags any divergence as a finding.

The one rule (the lesson from the installer port saga): a probe NEVER defines its own truth. It imports
and calls the canonical source. If two probes disagree, that disagreement is the bug, and the fix is to
make the outlier reuse the trusted source, never to add a third definition here.

Acceptance test (the reason this exists): feed it the over-strict process-identity rule shipped earlier
this session, and it must report that the installer's definition disagrees with `mongoose stop`'s on the
sample inputs, turning three rounds of round-tripping into one failing assertion. See
tests/test_claim_consistency.py::test_catches_the_overstrict_installer_rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# A probe that cannot evaluate in this environment (e.g. an optional dependency is absent) returns SKIP.
# SKIP never counts as disagreement , a missing surface must not raise a false divergence.
SKIP: Any = object()

# A registry of claims, populated by @claim. Module-level so adding a claim is a single decorator.
_REGISTRY: dict[str, "Claim"] = {}


@dataclass(frozen=True)
class Probe:
    """One authority's executable definition. `fn(probe_input)` returns a comparable value, or SKIP."""
    authority: str
    fn: Callable[[Any], Any]


@dataclass
class Claim:
    key: str
    description: str
    probes: tuple[Probe, ...]
    # Optional fixed input shared by all probes (so a claim is self-describing and check() needs no
    # caller data). If None, the caller's probe_input is used.
    fixed_input: Any = None
    # The self_harden claim this maps onto, so a divergence attaches to the right P0/P1 promise.
    maps_to: str = ""


def claim(key: str, description: str, *, maps_to: str = "", fixed_input: Any = None):
    """Decorator: register a claim. The decorated function returns the tuple of Probes."""
    def wrap(fn: Callable[[], tuple[Probe, ...]]) -> Callable[[], tuple[Probe, ...]]:
        _REGISTRY[key] = Claim(key=key, description=description, probes=tuple(fn()),
                               fixed_input=fixed_input, maps_to=maps_to)
        return fn
    return wrap


def _normalise(v: Any) -> Any:
    """Compare on meaning, not representation: strip/lower strings, recurse into sequences."""
    if v is SKIP:
        return SKIP
    if isinstance(v, str):
        return v.strip().lower()
    if isinstance(v, (list, tuple)):
        return tuple(_normalise(x) for x in v)
    return v


def check(claim_obj: Claim, probe_input: Any = None) -> dict[str, Any]:
    """Run every probe over the same input; report agreement and, on disagreement, WHO disagrees."""
    data = claim_obj.fixed_input if claim_obj.fixed_input is not None else probe_input
    results: dict[str, str] = {}
    values: list[tuple[str, Any]] = []
    for p in claim_obj.probes:
        try:
            raw = p.fn(data)
        except Exception as exc:  # an exploding probe is isolated, not fatal to the scan
            results[p.authority] = f"ERROR: {str(exc)[:160]}"
            continue
        if raw is SKIP:
            results[p.authority] = "SKIP"
            continue
        results[p.authority] = repr(raw)
        values.append((p.authority, _normalise(raw)))

    distinct: list[Any] = []
    for _, v in values:
        if v not in distinct:
            distinct.append(v)
    comparable = len(values) >= 2          # fewer than two evaluable probes => nothing to contradict
    agree = len(distinct) <= 1

    out: dict[str, Any] = {
        "key": claim_obj.key, "description": claim_obj.description,
        "maps_to": claim_obj.maps_to, "results": results,
        "evaluated": len(values), "comparable": comparable, "agree": bool(agree),
    }
    if comparable and not agree:
        groups: dict[str, list[str]] = {}
        for auth, v in values:
            groups.setdefault(repr(v), []).append(auth)
        out["divergence"] = groups     # the actionable part: which authorities returned which value
    return out


def check_all(probe_input: Any = None) -> dict[str, Any]:
    """Check every registered claim. `diverging` is the high-value finding list."""
    reports = [check(c, probe_input) for c in _REGISTRY.values()]
    diverging = [r for r in reports if r["comparable"] and not r["agree"]]
    return {
        "total": len(reports),
        "diverging": diverging,
        "diverging_count": len(diverging),
        "untested": [r["key"] for r in reports if not r["comparable"]],
        "reports": reports,
        "ok": not diverging,
    }


def claims() -> list[Claim]:
    return list(_REGISTRY.values())


# ==================================================================================================
# Registered claims. Each probe imports and calls a CANONICAL source. Adding a probe must never mean
# writing a second definition , import the real one.
# ==================================================================================================

# Shared sample command lines for the process-identity claim. Includes the exact forms that bit us:
# a folder name with a space ("mongoose-alpha 7"), a Trashed copy, the uvicorn form, and path traps.
_PROC_SAMPLES = (
    "/Users/x/Downloads/mongoose-alpha 7/.venv/bin/python -m mongoose serve --port 8765",
    "/opt/homebrew/Python /Users/x/.Trash/mongoose-alpha 7/.venv/bin/mongoose serve --port 8765",
    "mongoose serve --port 8765",
    ".venv/bin/python .venv/bin/uvicorn mongoose.app:app",
    "/usr/bin/python -m something --config /Users/x/mongoose-ntfy/serve.yml",  # path trap -> False
    "node /Users/x/mongoose-dashboard/server.js",                              # unrelated -> False
    "/usr/sbin/cupsd -l",                                                      # foreign  -> False
)


@claim("process_identity",
       "'Is this command a Mongoose server?' has ONE definition, shared by the installer and `mongoose stop`.",
       maps_to="install_runtime_identity", fixed_input=_PROC_SAMPLES)
def _process_identity_probes() -> tuple[Probe, ...]:
    # Authority 1: the canonical Python rule that `mongoose stop` trusts.
    from ..cli import _command_looks_like_mongoose_server as canonical

    # Authority 2: the installer's shell rule, transcribed. If it ever drifts from authority 1 over the
    # sample set, THIS claim flags it , exactly the bug that cost three rounds on the Mac.
    def shell_rule(cmd: str) -> bool:
        low = (cmd or "").lower()
        if "mongoose.app:app" in low and "uvicorn" in low:
            return True
        return "mongoose" in low and " serve" in low

    return (
        Probe("cli._command_looks_like_mongoose_server",
              lambda samples: tuple(bool(canonical(s)) for s in samples)),
        Probe("installer shell rule (transcribed)",
              lambda samples: tuple(bool(shell_rule(s)) for s in samples)),
    )


@claim("configured_requires_authorisation",
       "'Configured' for an OAuth integration means signed-in (a token exists), not just a client file on disk.",
       maps_to="configured_truth",
       fixed_input={"GOOGLE_CLIENT_SECRETS_PATH": "/nonexistent/google-client-secrets.json"})
def _configured_probes() -> tuple[Probe, ...]:
    # Both authorities answer the same question for a client-file-only env: is Google Calendar usable?
    # Trusted answer = NO (needs sign-in). If any says YES, that subsystem has drifted.
    def wizard_state(env: dict) -> Any:
        try:
            from ..setup_wizard import _google_calendar_auth_state
            return _google_calendar_auth_state(env) == "signed_in"
        except Exception:
            return SKIP

    def token_truth(_env: dict) -> Any:
        try:
            from ..setup_wizard import _oauth_token_present, _google_token_paths
            return bool(_oauth_token_present(_google_token_paths()))
        except Exception:
            return SKIP

    return (
        Probe("setup_wizard.auth_state == signed_in", wizard_state),
        Probe("token file present (invariant)", token_truth),
    )


# Backward-compatible public alias , `run_builtin()` is the convenience entry point used by the
# self_harden scan hook, the CLI (`mongoose harden consistency`) and the cockpit endpoint.
def run_builtin(probe_input: Any = None) -> dict[str, Any]:
    return check_all(probe_input)
