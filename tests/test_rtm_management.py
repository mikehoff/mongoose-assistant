import csv
import os


def test_demand_register_ledger_and_exact_metro_reconciliation(tmp_path):
    from mongoose.services import rtm_management as M

    M.seed_flats()
    M.upsert_flat(block="1EC", flat_number=1, leaseholder_name="Jane Leaseholder", payment_reference="JANE")
    d = M.create_demand(document_number="26-TO-DEC/1ECF1R0", flat="1ECF1", period="Half-year ending 31 Dec 2026",
                        amount="901.23", issue_date="2026-07-16", delivery_method="Docmail")
    assert d["due_date"] == "2026-08-13"
    st = M.flat_statement("1ECF1")
    assert st["balance"] == "£901.23"
    assert st["position"] == "debit"

    csv_path = tmp_path / "metro.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Date", "Description", "Reference", "Amount"])
        w.writeheader()
        w.writerow({"Date": "2026-07-20", "Description": "JANE service charge", "Reference": "JANE", "Amount": "901.23"})
    out = M.import_metro_csv(csv_path)
    assert out["exact"] == 1
    out2 = M.import_metro_csv(csv_path)
    assert out2["duplicates_or_skipped"] == 1
    assert M.allocation_review_queue()["transactions"][0]["match_status"] == "duplicate"
    demand = M.list_demands()[0]
    assert demand["status"] == "Paid"
    assert demand["outstanding"] == "£0.00"
    st2 = M.flat_statement("1ECF1")
    assert st2["balance"] == "£0.00"


def test_partial_and_unmatched_payments_go_to_review_and_manual_split_allocation(tmp_path):
    from mongoose.services import rtm_management as M

    M.seed_flats()
    M.upsert_flat(block="1EC", flat_number=2, leaseholder_name="Ben", payment_reference="BEN")
    M.create_demand(document_number="D1", flat="1ECF2", period="old", amount="100.00", issue_date="2026-07-01")
    M.create_demand(document_number="D2", flat="1ECF2", period="new", amount="200.00", issue_date="2026-07-02")
    csv_path = tmp_path / "metro.csv"
    csv_path.write_text("Date,Description,Reference,Amount\n2026-07-03,BEN combined,BEN,300.00\n2026-07-04,UNKNOWN,,50.00\n")
    out = M.import_metro_csv(csv_path)
    assert out["partial"] == 1
    assert out["unmatched"] == 1
    review = M.allocation_review_queue()
    assert review["count"] == 2
    tx = [t for t in review["transactions"] if t["reference"] == "BEN"][0]
    alloc = M.allocate_payment(tx["id"], [{"document_number": "D1", "amount": "100.00"}, {"document_number": "D2", "amount": "200.00"}])
    assert alloc["ok"]
    statuses = {d["document_number"]: d["status"] for d in M.list_demands()}
    assert statuses["D1"] == "Paid"
    assert statuses["D2"] == "Paid"


def test_arrears_reminder_release_and_referral_pack():
    from mongoose.services import rtm_management as M

    M.seed_flats()
    M.create_demand(document_number="OD1", flat="3ECF3", period="H1", amount="500.00", issue_date="2026-01-01", due_date="2026-01-29")
    arr = M.arrears(today="2026-03-30")
    assert arr["count"] == 1
    item = arr["items"][0]
    assert item["days_overdue"] == 60
    assert item["next_action"]["action_type"] == "final_notice"
    rem = M.generate_reminder("OD1", today="2026-03-30")
    assert rem["action_type"] == "final_notice"
    assert "Demand: OD1" in rem["text"]
    rel = M.release_recovery_action(rem["id"], "Docmail")
    assert rel["status"] == "Released"
    pack = M.referral_pack(document_number="OD1")
    assert pack["demands"]
    assert pack["recovery_actions"]


def test_payables_budget_actuals_cashflow_and_evidence(tmp_path):
    from mongoose.services import rtm_management as M

    M.set_budget_line(year="2026", schedule="sch1", budget_line="cleaning", amount="1200.00")
    p = M.add_payable(supplier="Cleaner Ltd", description="Monthly cleaning", schedule="sch1", amount="100.00",
                      invoice_date="2026-07-01", due_date="2026-07-31", budget_line="cleaning", vat_rate=20)
    M.approve_payable(p["id"])
    M.mark_payable_paid(p["id"], bank_reference="METRO-1", paid_date="2026-07-20")
    actuals = M.budget_actuals("2026")
    line = actuals["lines"][0]
    assert line["budgeted"] == "£1,200.00"
    assert line["spent"] == "£100.00"
    assert line["variance"] == "£1,100.00"
    assert M.cashflow()["horizons"]

    evidence_file = tmp_path / "quote.pdf"
    evidence_file.write_bytes(b"quote bytes")
    ev = M.add_evidence(target_type="budget_line", target_id="2026:sch1:cleaning",
                        document_kind="quote", title="Cleaning quote", path=str(evidence_file))
    assert ev["sha256"]
    assert M.list_evidence(target_type="budget_line")


def test_management_build_plan_is_a_valid_dag():
    from mongoose.services.build_plans import EXAMPLE_COURT_MANAGEMENT_STEPS

    ids = {s["id"] for s in EXAMPLE_COURT_MANAGEMENT_STEPS}
    assert len(ids) == len(EXAMPLE_COURT_MANAGEMENT_STEPS)
    for step in EXAMPLE_COURT_MANAGEMENT_STEPS:
        for dep in step.get("depends_on", []):
            assert dep in ids
        assert "verify" in step


def test_management_api_write_routes_require_cockpit_nonce():
    from fastapi.testclient import TestClient
    from mongoose.app import app

    client = TestClient(app)
    no_nonce = client.post("/rtm/manage/seed-flats")
    assert no_nonce.status_code == 403
    nonce = client.get("/cockpit/nonce").json()["cockpit_nonce"]
    ok = client.post("/rtm/manage/seed-flats", headers={"X-Cockpit-Nonce": nonce})
    assert ok.status_code == 200
    assert ok.json()["total"] == 18
    read = client.get("/rtm/manage/flats")
    assert read.status_code == 200
    assert len(read.json()["flats"]) == 18


# --- regression tests for the two FTT-critical bugs fixed during the merge ----

def _fresh_mgmt():
    import os, tempfile
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "mongoose.sqlite3")
    from mongoose.db import init_db
    init_db()


def test_money_integer_is_pounds_not_guessed():
    """REGRESSION: the original code guessed an int >1000 was already pence, so 1500 read as £15.
    Money must treat integers as pounds consistently."""
    _fresh_mgmt()
    from mongoose.services import rtm_management as M
    assert M._money_to_pence(1500) == 150000      # £1,500.00, not £15.00
    assert M._money_to_pence(500) == 50000        # £500.00
    assert M._money(M._money_to_pence(1500)) == "\u00a31,500.00"


def test_money_decimal_no_float_drift():
    """REGRESSION: money uses Decimal, not float round(), so tricky amounts are exact."""
    _fresh_mgmt()
    from mongoose.services import rtm_management as M
    assert M._money_to_pence("750.10") == 75010
    assert M._money_to_pence(0.29) == 29
    assert M._money_to_pence("1,234.56") == 123456   # commas + exact


def test_superseding_demand_reverses_old_ledger_debit():
    """REGRESSION: superseding a demand previously left BOTH debits in the ledger, double-counting
    the balance. The superseded demand's debit must be reversed (not deleted) so the per-flat
    balance reflects only the live demand, with the full audit trail kept."""
    _fresh_mgmt()
    from mongoose.services import rtm_management as M
    M.seed_flats()
    M.create_demand(document_number="R0", flat="1ECF1", period="2026", amount=800,
                    issue_date="2026-06-01")
    M.create_demand(document_number="R1", flat="1ECF1", period="2026", amount=850,
                    issue_date="2026-06-01", supersedes="R0")
    st = M.flat_statement("1ECF1")
    assert st["balance_pence"] == 85000   # only the live £850, not £1,650
    # the audit trail keeps the original debit + the reversal (nothing deleted)
    kinds = [e["kind"] for e in st["entries"]]
    assert kinds.count("demand") == 2
    assert "reversal" in kinds


# --- v0.49 dashboard slice: bulk import inherits the spine's money/immutability fixes ----

def test_import_demands_csv_inherits_money_fix():
    """The bulk demand importer goes through create_demand, so it must inherit the Decimal money
    fix: an integer amount in a CSV (1500) is £1,500.00, not £15.00. This is the real-data path,
    so the fix matters most here."""
    _fresh_mgmt()
    import csv as _csv
    from mongoose.services import rtm_management as M
    M.seed_flats()
    p = os.path.join(os.environ["MONGOOSE_DATA_DIR"], "d.csv")
    with open(p, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["document_number", "flat", "period", "amount", "issue_date"])
        w.writerow(["IMP-1", "1ECF1", "2026", "1500", "2026-01-01"])      # integer
        w.writerow(["IMP-2", "1ECF2", "2026", "750.10", "2026-01-01"])    # decimal
    r = M.import_demands_csv(p)
    assert r["imported"] == 2 and not r["errors"]
    assert M.flat_statement("1ECF1")["balance_pence"] == 150000   # £1,500.00
    assert M.flat_statement("1ECF2")["balance_pence"] == 75010    # £750.10


def test_import_demands_csv_skips_existing_not_overwrite():
    """Re-importing a document number is skipped, not overwritten , preserving the audit trail."""
    _fresh_mgmt()
    import csv as _csv
    from mongoose.services import rtm_management as M
    M.seed_flats()
    p = os.path.join(os.environ["MONGOOSE_DATA_DIR"], "d.csv")
    with open(p, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["document_number", "flat", "period", "amount", "issue_date"])
        w.writerow(["DUP-1", "1ECF1", "2026", "500", "2026-01-01"])
    M.import_demands_csv(p)
    r2 = M.import_demands_csv(p)   # same file again
    assert r2["skipped_existing"] == 1 and r2["imported"] == 0


def test_import_flats_csv_upserts():
    _fresh_mgmt()
    import csv as _csv
    from mongoose.services import rtm_management as M
    p = os.path.join(os.environ["MONGOOSE_DATA_DIR"], "f.csv")
    with open(p, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["block", "flat", "leaseholder", "reference"])
        w.writerow(["1EC", "1", "Jane Smith", "SMITH"])
    r = M.import_flats_csv(p)
    assert r["imported"] == 1 and not r["errors"]
    assert any(fl["flat_id"] == "1ECF1" for fl in M.list_flats())


def test_management_dashboard_shape():
    _fresh_mgmt()
    from mongoose.services import rtm_management as M
    M.seed_flats()
    d = M.management_dashboard()
    for key in ("status", "arrears", "review_queue", "payables_due_soon", "next_actions"):
        assert key in d
    assert isinstance(d["next_actions"], list) and d["next_actions"]


def test_export_management_pack_writes_files():
    _fresh_mgmt()
    from mongoose.services import rtm_management as M
    M.seed_flats()
    out = os.path.join(os.environ["MONGOOSE_DATA_DIR"], "pack")
    r = M.export_management_pack(out, year="2026")
    assert r["ok"] and r["file_count"] >= 10
    assert os.path.exists(os.path.join(out, "demands.csv"))
    assert os.path.exists(os.path.join(out, "dashboard.json"))
    assert os.path.exists(os.path.join(out, "README.md"))
