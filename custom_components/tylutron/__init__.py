"""The Tylutron integration."""
import logging
from typing import Any
import os
from homeassistant.util import Throttle
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant

from .const import DEFAULT_PASSWORD, DEFAULT_USERNAME, DOMAIN
from .lutronlib import Lutron

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CLIMATE]

MIN_TIME_BETWEEN_UPDATES = timedelta(hours=24)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tylutron from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Create cache directory if it doesn't exist
    cache_dir = hass.config.path("custom_components", DOMAIN, "cache")
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)

    cache_file = os.path.join(cache_dir, f"{entry.entry_id}_db.xml")

    lutron = Lutron(
        entry.data[CONF_HOST],
        entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
        entry.data.get(CONF_PASSWORD, DEFAULT_PASSWORD),
    )

    try:
        await hass.async_add_executor_job(lutron.connect)
        await hass.async_add_executor_job(lutron.load_xml_db, cache_file)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.error("Error connecting to Lutron hub: %s", str(ex))
        return False

    hass.data[DOMAIN][entry.entry_id] = lutron

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok 