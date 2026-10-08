"""Role-aware WhatsApp account model.

Mongoose can drive two WAHA sessions with distinct purposes:

    owner_read     The owner's personal WhatsApp, read/indexed so Mongoose can answer questions about his inbox.
    mongoose_send  Mongoose's OWN WhatsApp number, used to send notifications and approved messages as Mongoose.

Backward compatibility: a single-session install (only WHATSAPP_SESSION set) maps BOTH roles to that one
session, so nothing breaks. Dual mode activates when WHATSAPP_OWNER_SESSION and/or WHATSAPP_MONGOOSE_SESSION
are set to different names.

This module is deliberately free of I/O: it just resolves configuration into Account records that the
provider, journey, cockpit and CLI all agree on.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

OWNER_READ = "owner_read"
MONGOOSE_SEND = "mongoose_send"
ROLES = (OWNER_READ, MONGOOSE_SEND)

_LEGACY_DEFAULT = "default"


@dataclass
class Account:
    role: str
    session: str
    label: str
    number: str = ""

    def to_dict(self) -> dict:
        return {"role": self.role, "session": self.session, "label": self.label, "number": self.number}


def _owner_session() -> str:
    # Explicit owner session wins; else the legacy single session; else the site profile's default.
    explicit = os.getenv("WHATSAPP_OWNER_SESSION") or os.getenv("WHATSAPP_SESSION")
    if explicit:
        return explicit
    from .. import profile as _profile
    return _profile.get("owner.whatsapp_session")


def _mongoose_session() -> str:
    # If an explicit mongoose session is set, use it. Otherwise, in a legacy single-session install we
    # deliberately reuse the owner session so sends keep working on one account.
    explicit = os.getenv("WHATSAPP_MONGOOSE_SESSION")
    if explicit:
        return explicit
    if os.getenv("WHATSAPP_SESSION") and not os.getenv("WHATSAPP_OWNER_SESSION"):
        return os.getenv("WHATSAPP_SESSION", _LEGACY_DEFAULT)
    return "mongoose"


def owner_number() -> str:
    return (os.getenv("WHATSAPP_OWNER_NUMBER") or os.getenv("WHATSAPP_SELF_NUMBER") or "").strip()


def mongoose_number() -> str:
    return (os.getenv("WHATSAPP_MONGOOSE_NUMBER") or "").strip()


def _owner_inbox_label() -> str:
    from .. import profile as _profile
    return f"{_profile.owner_name()}'s WhatsApp inbox"


def account_for_role(role: str) -> Account:
    if role == MONGOOSE_SEND:
        return Account(role=MONGOOSE_SEND, session=_mongoose_session(),
                       label="Mongoose notifications", number=mongoose_number())
    # default/owner
    return Account(role=OWNER_READ, session=_owner_session(),
                   label=_owner_inbox_label(), number=owner_number())


def session_for_role(role: str) -> str:
    return account_for_role(role).session


def accounts() -> list[Account]:
    return [account_for_role(OWNER_READ), account_for_role(MONGOOSE_SEND)]


def role_for_session(session: str) -> str:
    """Map an inbound WAHA session name back to a role, so stored messages can be tagged."""
    s = (session or "").strip()
    if not s:
        return ""
    if s == _mongoose_session():
        return MONGOOSE_SEND
    if s == _owner_session():
        return OWNER_READ
    return ""


def is_dual_mode() -> bool:
    return _owner_session() != _mongoose_session()


def summary() -> dict:
    owner, mongoose = account_for_role(OWNER_READ), account_for_role(MONGOOSE_SEND)
    return {
        "dual_mode": is_dual_mode(),
        "owner_session": owner.session,
        "mongoose_session": mongoose.session,
        "owner_number": owner.number,
        "mongoose_number": mongoose.number,
    }
