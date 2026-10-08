
from mongoose.updater import current_version, version_of


def test_current_version_matches_package():
    from mongoose import __version__
    assert current_version() == __version__


def test_version_of_reads_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "9.9.9"\n')
    assert version_of(tmp_path) == "9.9.9"


def test_version_of_missing_returns_none(tmp_path):
    assert version_of(tmp_path) is None
