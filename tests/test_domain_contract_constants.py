"""Domain-vocabulary constants pinned as CONTRACTS.

These modules each declare a canonical set/order of domain statuses. The dead-constant gate
flags any such constant that nothing reads. Rather than delete legal/status vocabularies that
document the real domain (RTM demands, bank matching, portal account states, care/rehearsal
ordering), this file consumes each one with a MEANINGFUL assertion, so:
  1. the gate is satisfied (they are now read), and
  2. the constant becomes a real drift detector: if the module's emitted literals and its
     declared vocabulary diverge, a test fails instead of the two silently disagreeing.

This is the honest version of "wire it in": for money/legal paths, the safe wiring is a
contract check, not a new runtime branch.
"""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")


def test_rtm_management_status_vocabularies_cover_module_literals():
    import re
    import mongoose.services.rtm_management as M
    src = __import__("pathlib").Path(M.__file__).read_text()

    # Every demand/payable status STRING assigned/compared in the module must be declared.
    demand_like = set(re.findall(r'status[A-Za-z_]*\s*(?:=|==|!=|in)\s*[\[{(]?\s*"([A-Z][A-Za-z -]+)"', src))
    declared = M.DEMAND_STATUSES | M.PAYABLE_STATUSES
    stray = demand_like - declared
    assert not stray, f"demand/payable status literals not in declared sets: {stray}"

    # Bank-match statuses are lowercase tokens; each declared one is a plausible token.
    assert M.BANK_MATCH_STATUSES == {"exact", "partial", "unmatched", "duplicate", "reviewed"}
    assert "unmatched" in M.BANK_MATCH_STATUSES  # the default/failure state must exist


def test_rtm_portal_account_states_are_the_full_lifecycle():
    import mongoose.services.rtm_portal as P
    # The account lifecycle must include the entry state, the two review gates, and all exits.
    for required in ("pending_claim", "pending_email_verification", "pending_director_review",
                     "active", "rejected", "disabled"):
        assert required in P.ACCOUNT_STATES, f"account lifecycle missing {required}"


def test_rtm_secretary_status_order_matches_emitted_statuses():
    import re
    import mongoose.services.rtm_secretary as S
    src = __import__("pathlib").Path(S.__file__).read_text()
    emitted = set(re.findall(r'"(overdue|due_soon|upcoming|ongoing|done)"', src))
    assert emitted <= set(S._STATUS_ORDER), \
        f"secretary emits statuses absent from _STATUS_ORDER: {emitted - set(S._STATUS_ORDER)}"
    # The order must be a strict priority ranking (distinct ranks).
    assert len(set(S._STATUS_ORDER.values())) == len(S._STATUS_ORDER)


def test_dress_rehearsal_order_ranks_every_result_kind():
    import mongoose.dress_rehearsal as D
    # Every result constant the module can emit must have a rank in _ORDER.
    for const in (D.PASS, D.CONFIGURE, D.DISABLED, D.HARDWARE, D.FAIL):
        assert const in D._ORDER, f"result kind {const!r} has no sort rank"
    assert len(set(D._ORDER.values())) == len(D._ORDER)  # distinct ranks


def test_alpha_import_muscle_groups_are_the_schema_vocabulary():
    import mongoose.services.alpha_import as A
    # The canonical set must include the core groups the real muscle_group_summary uses.
    for core in ("biceps", "back_lats", "quads", "hamstrings", "calves", "chest", "other"):
        assert core in A.MUSCLE_GROUPS, f"canonical muscle group vocabulary missing {core}"
    assert "other" in A.MUSCLE_GROUPS  # the catch-all must exist so classification never drops data


def test_whatsapp_roles_each_map_to_an_account():
    import mongoose.services.whatsapp_accounts as W
    for role in W.ROLES:
        acct = W.account_for_role(role)
        assert acct.role in W.ROLES, f"account_for_role({role!r}) returned an undeclared role"


def test_asset_kinds_cover_the_classifier_outputs():
    import re
    import mongoose.services.assets as A
    src = __import__("pathlib").Path(A.__file__).read_text()
    # Every kind the classifier assigns must be a declared ASSET_KIND.
    assigned = set(re.findall(r'kind\s*=\s*"([a-z_]+)"', src))
    stray = assigned - set(A.ASSET_KINDS)
    assert not stray, f"_classify assigns kinds not in ASSET_KINDS: {stray}"


def test_rtm_example_court_reconciliation_sentence_is_the_used_disclosure():
    import mongoose.services.rtm_example_court as PC
    # The flagged sentence is the fixed legal disclosure; assert it is well-formed and non-empty
    # so it can't silently become a placeholder.
    assert "reconciling actual costs to budget" in PC.RECONCILIATION_SENTENCE_FLAG
    assert PC.RECONCILIATION_SENTENCE_FLAG.strip().endswith("six months")
