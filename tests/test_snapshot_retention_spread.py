"""Brain snapshots must be spread across time, not seven copies of the same week.

Keeping the newest N in a row means that if a problem is noticed a fortnight later, every surviving
snapshot already contains it. Tiering the same budget across days/weeks/months reaches back months
for identical disk cost.
"""
from datetime import datetime, timedelta

import pytest

from mongoose import brain_preservation as BP


@pytest.fixture()
def snaps(tmp_path, monkeypatch):
    """40 consecutive daily snapshots plus an old preupdate."""
    base = tmp_path / "brain_snapshots"
    base.mkdir(parents=True)
    monkeypatch.setattr(BP, "brain_paths", lambda: {"data_dir": tmp_path})
    start = datetime(2026, 7, 28, 3, 30)
    for i in range(40):
        d = start - timedelta(days=i)
        (base / f"daily-{d.strftime('%Y%m%dT%H%M%S')}").mkdir()
    (base / "preupdate-20260601T010000").mkdir()
    return base


def test_kept_set_spans_far_more_than_a_week(snaps):
    r = BP.prune_snapshots(keep=7, dry_run=True)
    stamps = sorted(BP._snapshot_stamp(n) for n in r["kept_names"])
    span_days = (stamps[-1] - stamps[0]).days
    assert span_days > 30, f"kept snapshots only span {span_days} days; that is not spread"


def test_the_newest_is_always_kept(snaps):
    r = BP.prune_snapshots(keep=7, dry_run=True)
    assert "daily-20260728T033003" in r["kept_names"] or any(
        n.startswith("daily-20260728") for n in r["kept_names"])


def test_the_oldest_is_kept_so_deep_history_survives(snaps):
    r = BP.prune_snapshots(keep=7, dry_run=True)
    assert any(n.startswith("preupdate-20260601") for n in r["kept_names"]), \
        "the deepest recovery point must survive a prune"


def test_a_preupdate_snapshot_is_protected(snaps):
    """The state immediately before a risky change is what you reach for when the change broke it."""
    r = BP.prune_snapshots(keep=7, dry_run=True)
    assert any(n.startswith("preupdate") for n in r["kept_names"])


def test_dry_run_deletes_nothing(snaps):
    before = len(list(snaps.iterdir()))
    BP.prune_snapshots(keep=7, dry_run=True)
    assert len(list(snaps.iterdir())) == before, "a dry run must never remove a directory"


def test_ordering_is_by_date_not_by_name(snaps):
    """'preupdate-' sorts above 'daily-' as a string, which once made the OLDEST look like the newest."""
    r = BP.prune_snapshots(keep=7, dry_run=True)
    newest_kept = max(BP._snapshot_stamp(n) for n in r["kept_names"])
    assert newest_kept.year == 2026 and newest_kept.month == 7 and newest_kept.day == 28


def test_spread_false_restores_newest_n(snaps):
    r = BP.prune_snapshots(keep=5, spread=False, dry_run=True)
    stamps = sorted((BP._snapshot_stamp(n) for n in r["kept_names"]), reverse=True)
    assert (stamps[0] - stamps[-1]).days <= 5, "spread=False should keep a consecutive run"


def test_undateable_directory_is_never_deleted(snaps):
    (snaps / "mystery-folder").mkdir()
    r = BP.prune_snapshots(keep=7, dry_run=True)
    assert "mystery-folder" not in r["would_delete"], "never delete what you cannot date"
