"""/install sends Clear-Site-Data so a browser that DOES get through to the real server (as
opposed to being intercepted by an already-stale worker) is forced to drop any leftover
service worker / cache state automatically, no manual Develop-menu unregister required.
Scoped narrowly: NOT sent on /welcome or any paired surface, since those may legitimately
need to read an existing device token from localStorage on that same origin."""
from __future__ import annotations

import re
import warnings

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient

import mongoose.app as A


def test_install_sends_well_formed_clear_site_data():
    r = TestClient(A.app).get("/install")
    val = r.headers.get("clear-site-data")
    assert val == '"cache", "storage"'
    assert re.fullmatch(r'"[a-z]+"(, "[a-z]+")*', val)


def test_clear_site_data_not_sent_on_paired_surfaces():
    c = TestClient(A.app)
    for path in ("/welcome", "/device/launchpad"):
        r = c.get(path)
        assert "clear-site-data" not in {k.lower() for k in r.headers.keys()}, path
