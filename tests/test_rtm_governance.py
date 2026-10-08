from datetime import date


def test_company_secretary_computes_core_deadlines():
    from mongoose.services import rtm_secretary as S

    S.configure_profile(
        company_name="Example Court RTM Company Limited",
        company_number="01234567",
        incorporation_date="2026-01-10",
        accounting_reference_date="2026-12-31",
        confirmation_statement_date="2026-01-10",
        corporation_tax_period_end="2026-12-31",
    )
    rep = S.compute_compliance(today=date(2026, 6, 20))
    kinds = {i["kind"]: i for i in rep["items"]}
    assert kinds["confirmation_statement"]["due_date"] == "2027-01-24"
    assert kinds["companies_house_accounts"]["due_date"] == "2027-10-10"
    assert kinds["corporation_tax_payment"]["due_date"] == "2027-10-01"
    assert kinds["company_tax_return"]["due_date"] == "2027-12-31"
    assert kinds["service_charge_accounts"]["status"] == "ongoing"
    assert "Review registered office" in " ".join(kinds["confirmation_statement"]["guidance"])


def test_company_secretary_marks_done_and_exports(tmp_path):
    from mongoose.services import rtm_secretary as S

    S.configure_profile(incorporation_date="2026-01-10", confirmation_statement_date="2026-01-10")
    rep = S.compute_compliance(today=date(2026, 6, 20))
    item_id = [i for i in rep["items"] if i["kind"] == "confirmation_statement"][0]["id"]
    assert S.mark_done(item_id, "filed online")["ok"]
    rep2 = S.status_report()
    item = [i for i in rep2["items"] if i["id"] == item_id][0]
    assert item["status"] == "done"
    cal = tmp_path / "calendar.csv"
    assert S.export_calendar_csv(cal)["ok"]
    assert "confirmation_statement" in cal.read_text()
    pack = S.artifact_pack(tmp_path / "pack")
    assert pack["files"]


def test_bookkeeper_records_balances_transactions_transfers_and_reconciles(tmp_path):
    from mongoose.services import rtm_bookkeeper as B

    B.record_opening_balance("service", "1000.00", "2026-07-16")
    B.record_transaction(fund="service", direction="in", amount="250.00", occurred_on="2026-07-20",
                         description="Flat 1 service charge", category="service_charge_income",
                         counterparty="Flat 1", reference="SC-001")
    B.record_transaction(fund="service", direction="out", amount="120.00", occurred_on="2026-07-22",
                         description="Cleaning invoice", category="cleaning", counterparty="Cleaner")
    B.record_opening_balance("reserve", "500.00", "2026-07-16")
    B.record_transfer(from_fund="service", to_fund="reserve", amount="100.00", occurred_on="2026-07-25")
    B.record_bank_balance(fund="service", balance="1030.00", as_at="2026-07-26")
    B.record_bank_balance(fund="reserve", balance="600.00", as_at="2026-07-26")

    rec = B.reconcile()
    assert rec["overall"] == "ok"
    by_fund = {f["fund"]: f for f in rec["funds"]}
    assert by_fund["service_charge_trust"]["book_balance"] == "£1,030.00"
    assert by_fund["reserve_fund_trust"]["book_balance"] == "£600.00"
    out = tmp_path / "ledger.csv"
    assert B.export_csv(out)["ok"]
    text = out.read_text()
    assert "service_charge_trust" in text
    assert "reserve_fund_trust" in text


def test_bookkeeper_flags_reconciliation_mismatch():
    from mongoose.services import rtm_bookkeeper as B

    B.record_opening_balance("service", "100.00", "2026-07-16")
    B.record_bank_balance(fund="service", balance="99.00", as_at="2026-07-23")
    rec = B.reconcile()
    svc = [f for f in rec["funds"] if f["fund"] == "service_charge_trust"][0]
    assert svc["status"] == "mismatch"
    assert svc["difference"] == "-£1.00"


def test_build_plan_governance_is_valid_dag():
    from mongoose.services.build_plans import EXAMPLE_COURT_GOVERNANCE_STEPS

    ids = {s["id"] for s in EXAMPLE_COURT_GOVERNANCE_STEPS}
    assert len(ids) == len(EXAMPLE_COURT_GOVERNANCE_STEPS)
    for step in EXAMPLE_COURT_GOVERNANCE_STEPS:
        for dep in step.get("depends_on", []):
            assert dep in ids
        assert "verify" in step


# --- additional deadline-correctness regression tests (legally sensitive) -----
# These lock in the GOV.UK deadline arithmetic verified by hand during the merge:
# annual accounts = FYE + 9 months; CT payment = period end + 9 months + 1 day;
# CT return = period end + 12 months; confirmation statement = review date + 14 days.

def _fresh_gov():
    import os, tempfile
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()


def test_corporation_tax_deadlines_are_correct():
    _fresh_gov()
    from datetime import date
    from mongoose.services import rtm_secretary as S
    S.configure_profile(company_number="01234567", corporation_tax_period_end="2026-06-30")
    # today BEFORE the period end so the live period is the 2026-06-30 one
    rep = S.compute_compliance(today=date(2026, 6, 1))
    items = {i["kind"]: i for i in rep["items"]}
    # CT payment = period end + 9 months + 1 day = 2027-03-31
    assert items["corporation_tax_payment"]["due_date"] == "2027-03-31"
    # CT return = period end + 12 months = 2027-06-30
    assert items["company_tax_return"]["due_date"] == "2027-06-30"


def test_annual_accounts_deadline_is_fye_plus_nine_months():
    _fresh_gov()
    from datetime import date
    from mongoose.services import rtm_secretary as S
    # an established company (incorporated >21mo ago) so it's on the annual cycle
    S.configure_profile(company_number="01234567", incorporation_date="2020-01-01",
                        accounting_reference_date="2026-06-30")
    rep = S.compute_compliance(today=date(2026, 6, 1))
    items = {i["kind"]: i for i in rep["items"]}
    # accounts = period end (the 2026-06-30 one, since today is before it) + 9 months = 2027-03-30
    assert items["companies_house_accounts"]["due_date"] == "2027-03-30"


def test_missing_facts_produce_setup_items_not_fake_deadlines():
    _fresh_gov()
    from mongoose.services import rtm_secretary as S
    # no profile facts -> items should be 'needed'/ongoing, not invented dates
    rep = S.compute_compliance()
    items = {i["kind"]: i for i in rep["items"]}
    ct = items.get("company_tax_return", {})
    assert not ct.get("due_date") or "needed" in ct.get("title", "").lower()


def test_trust_funds_stay_separate():
    _fresh_gov()
    from mongoose.services import rtm_bookkeeper as B
    B.record_opening_balance("service", 1000, "2026-07-16")
    B.record_opening_balance("reserve", 500, "2026-07-16")
    B.record_transaction(fund="service", amount=250, direction="in",
                         description="Flat 1 service charge", category="service_charge_income")
    s = B.summary()
    bal = s["ledger_balances"]
    # service charge and reserve balances are tracked independently (s.42 trust separation)
    assert bal["service_charge_trust"] == "\u00a31,250.00"
    assert bal["reserve_fund_trust"] == "\u00a3500.00"
