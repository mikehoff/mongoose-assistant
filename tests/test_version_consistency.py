from pathlib import Path

from mongoose import __version__


def test_pyproject_version_matches_package_version():
    text = Path("pyproject.toml").read_text()
    assert f'version = "{__version__}"' in text


def test_cockpit_header_shows_the_real_version():
    """The audit found the cockpit hardcoding 1.2.0 while /health said 1.32.1. The header must
    render from __version__, the single source of truth, forever."""
    import mongoose
    from mongoose.cockpit import cockpit_html
    h = cockpit_html()
    assert mongoose.__version__ in h
    assert "1.2.0" not in h
    assert "__MONGOOSE_VERSION__" not in h  # placeholder must be substituted, not leaked


def test_persona_tells_the_true_version():
    """The persona hardcoded '1.1 Beta' under a heading literally titled 'always accurate,
    never guess', so the model confidently told users a false version. It must render from
    __version__ with no placeholder leakage, forever."""
    import mongoose
    from mongoose.persona import persona_card
    c = persona_card()
    assert mongoose.__version__ in c
    assert "1.1 Beta" not in c and "1.1 beta" not in c
    assert "__MONGOOSE_VERSION__" not in c
