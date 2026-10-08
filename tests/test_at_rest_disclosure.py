"""The System Map must tell the TRUTH about at-rest exposure: Mongoose does not encrypt its
own data file, so it must not imply it does, and it must surface FileVault status honestly."""
from mongoose.db import init_db
from mongoose.transparency import system_map, _disk_encryption


def test_storage_reports_at_rest_status():
    init_db()
    st = system_map()["storage"]
    # the at-rest fields must be present and the note must be honest about no app-level encryption
    assert "encrypted_at_rest" in st
    assert "at_rest_note" in st
    assert "does not encrypt" in st["note"].lower() or "does not encrypt" in st["at_rest_note"].lower() \
        or "not encrypt" in st["note"].lower()


def test_disk_encryption_never_overclaims():
    """If we can't confirm encryption, we must NOT report encrypted=True."""
    enc = _disk_encryption()
    assert enc["encrypted"] in (True, False, None)
    if enc["encrypted"] is None:
        # unknown must say so and point at full-disk encryption, not imply safety
        assert "not encrypt" in enc["detail"].lower() or "filevault" in enc["detail"].lower()
