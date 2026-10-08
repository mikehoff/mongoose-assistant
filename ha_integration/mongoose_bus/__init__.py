"""Mongoose Bus integration for Home Assistant.

Lets HA Assist send messages to the Mongoose bus (e.g. "leave a note for Claude
Code to check the boiler") and exposes a service to post briefs/tasks/alerts.
Mongoose runs on the Mac mini; this talks to its /bus endpoint over the LAN.

Install: copy this folder to <ha_config>/custom_components/mongoose_bus/, add the
config below to configuration.yaml, restart HA.

configuration.yaml:
    mongoose_bus:
      url: http://<mac-mini-ip>:8123-equivalent   # Mongoose host:port
      token: !secret mongoose_api_token            # if MONGOOSE_ALLOW_REMOTE
"""
from __future__ import annotations

import logging
import voluptuous as vol
from homeassistant.helpers import config_validation as cv
import homeassistant.helpers.aiohttp_client as aiohttp_client

DOMAIN = "mongoose_bus"
_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required("url"): cv.string,
        vol.Optional("token", default=""): cv.string,
    })
}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass, config):
    conf = config[DOMAIN]
    base = conf["url"].rstrip("/")
    token = conf["token"]

    async def handle_send(call):
        session = aiohttp_client.async_get_clientsession(hass)
        headers = {"X-Mongoose-Token": token} if token else {}
        payload = {
            "sender": call.data.get("from", "ha"),
            "to": call.data["to"],
            "subject": call.data["subject"],
            "body": call.data.get("body", ""),
            "type": call.data.get("type", "note"),
        }
        try:
            async with session.post(f"{base}/bus/send", json=payload, headers=headers) as r:
                if r.status != 200:
                    _LOGGER.error("Mongoose bus send failed: HTTP %s", r.status)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Mongoose bus unreachable: %s", exc)

    hass.services.async_register(DOMAIN, "send", handle_send, schema=vol.Schema({
        vol.Optional("from", default="ha"): cv.string,
        vol.Required("to"): cv.string,
        vol.Required("subject"): cv.string,
        vol.Optional("body", default=""): cv.string,
        vol.Optional("type", default="note"): vol.In(["brief", "task", "alert", "reply", "note"]),
    }))
    return True
