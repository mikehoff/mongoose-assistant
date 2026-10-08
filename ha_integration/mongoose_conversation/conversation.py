"""Conversation platform: the Mongoose Assist agent entity."""
from __future__ import annotations

import logging

from homeassistant.components import conversation
from homeassistant.components.conversation import (
    ConversationEntity,
    ConversationEntityFeature,
    ConversationInput,
    ConversationResult,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
import homeassistant.helpers.aiohttp_client as aiohttp_client

_LOGGER = logging.getLogger(__name__)
DOMAIN = "mongoose_conversation"


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    conf = hass.data.get(DOMAIN, {}).get("config", {})
    async_add_entities([MongooseAgent(hass, conf)])


class MongooseAgent(ConversationEntity):
    """Forwards Assist requests to Mongoose's /assist endpoint."""

    _attr_has_entity_name = True
    _attr_name = "Mongoose"
    # Declare control so HA exposes this agent as able to act on the home.
    _attr_supported_features = ConversationEntityFeature.CONTROL

    def __init__(self, hass: HomeAssistant, conf: dict) -> None:
        self.hass = hass
        self._base = str(conf.get("url", "")).rstrip("/")
        self._token = conf.get("token", "")
        self._attr_unique_id = "mongoose_agent"

    @property
    def supported_languages(self):
        return "*"

    async def _async_handle_message(
        self,
        user_input: ConversationInput,
        chat_log: "conversation.ChatLog",
    ) -> ConversationResult:
        speech = await self._ask_mongoose(
            user_input.text, user_input.conversation_id
        )
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(speech)
        return ConversationResult(
            conversation_id=user_input.conversation_id,
            response=response,
            continue_conversation=False,
        )

    async def _ask_mongoose(self, text: str, conversation_id: str | None) -> str:
        session = aiohttp_client.async_get_clientsession(self.hass)
        headers = {"X-Mongoose-Token": self._token} if self._token else {}
        payload = {"text": text, "conversation_id": conversation_id or "assist-ha"}
        try:
            async with session.post(
                f"{self._base}/assist", json=payload, headers=headers, timeout=30
            ) as r:
                if r.status != 200:
                    _LOGGER.error("Mongoose /assist HTTP %s", r.status)
                    return "Sorry, Mongoose returned an error."
                data = await r.json()
                return data.get("speech", "Done.")
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Mongoose unreachable: %s", exc)
            return "I couldn't reach Mongoose just now."
