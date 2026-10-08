"""Mongoose conversation agent for Home Assistant.

Registers a conversation agent that forwards Assist queries to Mongoose's
/assist endpoint, so your local Mongoose persona, memory, and model answer
voice/app requests, and device control flows through Mongoose's own resolver.

Install: copy this folder to <ha_config>/custom_components/mongoose_conversation/,
add the config below, restart HA, then select "Mongoose" as the conversation
agent in Settings -> Voice assistants.

configuration.yaml:
    mongoose_conversation:
      url: http://<mac-mini-ip>:8765
      token: !secret mongoose_api_token   # if MONGOOSE_ALLOW_REMOTE=true
"""
from __future__ import annotations

DOMAIN = "mongoose_conversation"


async def async_setup(hass, config):
    hass.data.setdefault(DOMAIN, {})
    if DOMAIN in config:
        hass.data[DOMAIN]["config"] = config[DOMAIN]
    # Forward to the conversation platform.
    from homeassistant.helpers.discovery import async_load_platform
    hass.async_create_task(
        async_load_platform(hass, "conversation", DOMAIN, {}, config)
    )
    return True
