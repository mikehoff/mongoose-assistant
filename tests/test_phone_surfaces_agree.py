"""Every phone surface must offer the same set of screens.

The Tasks screen shipped INVISIBLE. It was fully built: route, loader, view container, handlers, all
live and all proven working by executing the served bundle. But the phone has three independent
hardcoded lists of screens, and Tasks was added to two of them:

  cockpit.py  T        loadCockpitTab()      the launchpad Cockpit tab, WHAT THE OWNER TAPS
  cockpit.py  screens  launcher search index
  cockpit.py  AREAS    the /device/cockpit tile grid

Because the PWA's start_url is /device/launchpad, the owner only ever saw list one. He reinstalled the
PWA twice trying to fix a cache problem that did not exist. Reading the source proved nothing here:
each list looked right on its own. Only comparing them finds this.
"""
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "mongoose" / "cockpit.py"


def _text():
    return SRC.read_text()


def _launchpad_tab_screens():
    """The T array inside loadCockpitTab(): [['icon','Label','screen'], ...]"""
    m = re.search(r"var T=\[(.*?)\];", _text(), re.S)
    assert m, "loadCockpitTab's tile array T not found; the test needs updating with the refactor"
    return set(re.findall(r"',\s*'([a-z_]+)'\]", m.group(1)))


def _cockpit_grid_screens():
    """The AREAS array: {ic:..,lb:..,screen:'x'} entries (ignoring `open:` jump-outs)."""
    m = re.search(r"var AREAS=\[(.*?)\n\];", _text(), re.S)
    assert m, "AREAS array not found"
    return set(re.findall(r"screen:'([a-z_]+)'", m.group(1)))


def _search_index_screens():
    """The launcher `screens` array: ['Label','screen','keywords']"""
    m = re.search(r"var screens=\[(.*?)\]\];", _text(), re.S)
    assert m, "launcher screens array not found"
    return set(re.findall(r"\['[^']+','([a-z_]+)'", m.group(1)))


def _loader_screens():
    """LOADERS maps screen -> render function; a screen with no loader would open blank."""
    m = re.search(r"var LOADERS=\{(.*?)\};", _text(), re.S)
    assert m, "LOADERS map not found"
    return set(re.findall(r"([a-z_]+):", m.group(1)))


def test_the_tab_the_owner_taps_offers_every_screen():
    """This is the exact failure: Tasks was missing from T and nowhere else."""
    missing = _cockpit_grid_screens() - _launchpad_tab_screens()
    assert not missing, (
        f"screens reachable on /device/cockpit but NOT from the launchpad Cockpit tab: {sorted(missing)}. "
        "The PWA opens /device/launchpad, so a screen missing here is invisible to the owner.")


def test_search_can_find_every_screen():
    missing = _cockpit_grid_screens() - _search_index_screens()
    assert not missing, f"screens absent from the launcher search index: {sorted(missing)}"


def test_every_offered_screen_has_a_loader():
    """A tile that opens an empty pane is worse than no tile."""
    for name, offered in (("launchpad tab", _launchpad_tab_screens()),
                          ("cockpit grid", _cockpit_grid_screens())):
        missing = offered - _loader_screens()
        assert not missing, f"{name} offers {sorted(missing)} with no loader; those panes open blank"


def test_every_offered_screen_has_a_view_container():
    """go(view) does getElementById('v-'+view).classList.add('on'); a missing container throws."""
    containers = set(re.findall(r'id="v-([a-z_]+)"', _text()))
    missing = (_launchpad_tab_screens() | _cockpit_grid_screens()) - containers
    assert not missing, f"no <section id='v-...'> for {sorted(missing)}; go() would throw on tap"


def test_tasks_specifically_is_reachable_from_the_phone():
    """Named explicitly because it regressed three times."""
    assert "tasks" in _launchpad_tab_screens(), "the Tasks tile must be on the tab the owner opens"
    assert "tasks" in _cockpit_grid_screens()
    assert "tasks" in _search_index_screens()
    assert "tasks" in _loader_screens()


@pytest.mark.parametrize("screen", sorted(_cockpit_grid_screens()))
def test_no_screen_is_offered_in_only_one_place(screen):
    """Divergence in either direction is drift; catch it whichever list gains an entry first."""
    where = [n for n, s in (("tab", _launchpad_tab_screens()),
                            ("grid", _cockpit_grid_screens()),
                            ("search", _search_index_screens())) if screen in s]
    assert len(where) >= 2, f"{screen} appears only in {where}; the phone surfaces have drifted"
