from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# Provider abstraction.
#
# The rest of Mongoose must NOT depend on any provider's raw payload shape (WAHA
# today, Baileys later). Everything flows through this normalised model and the
# WhatsAppProvider interface, so swapping or adding a transport never touches the
# index, the brief, what-changed, or the approval outbox.


@dataclass
class NormalisedWhatsAppMessage:
    """Mongoose's own message model. Providers translate their payloads into this."""
    provider: str
    session: str
    provider_message_id: str
    chat_id: str
    sender_id: str = ""
    sender_name: str = ""
    direction: str = "inbound"          # inbound | outbound
    text: str = ""
    timestamp: str = ""
    has_media: bool = False
    media_kind: str = ""                # image/video/audio/document/none
    media_filename: str = ""
    media_mime: str = ""
    media_size: int = 0
    quoted_message_id: str = ""
    privacy: str = "high"               # social messages default to high
    raw: dict[str, Any] = field(default_factory=dict)


class WhatsAppProvider(Protocol):
    """The contract every transport implements. Send is the only privileged
    operation, and it is only ever called from the approved-outbox path."""

    def status(self) -> dict[str, Any]: ...
    def send_text(self, chat_id: str, text: str) -> dict[str, Any]: ...
    def fetch_media(self, message_id: str) -> bytes | None: ...
    def normalise_webhook(self, payload: dict[str, Any]) -> list[NormalisedWhatsAppMessage]: ...


def get_provider(name: str | None = None, *, role: str | None = None):
    """Return the configured provider instance, bound to a role's WAHA session when given.

    role="owner_read" -> the owner's inbox session; role="mongoose_send" -> Mongoose's sender session.
    With no role, the provider uses the legacy single session, so existing installs are unchanged.
    Unknown/absent providers return None so callers degrade gracefully (draft-only, no transport)."""
    import os
    name = (name or os.getenv("WHATSAPP_PROVIDER") or "waha").lower()
    if name == "waha":
        from .waha import WahaProvider
        session = None
        if role:
            from ..whatsapp_accounts import session_for_role
            session = session_for_role(role)
        return WahaProvider(session=session)
    return None
