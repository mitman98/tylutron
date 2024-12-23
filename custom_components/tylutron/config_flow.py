"""Config flow for Tylutron integration."""
import logging
from typing import Any, Dict, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv

from .const import DEFAULT_PASSWORD, DEFAULT_USERNAME, DOMAIN
from .lutronlib import Lutron

_LOGGER = logging.getLogger(__name__)

class TylutronConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tylutron."""

    VERSION = 1

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            try:
                # Validate the connection
                lutron = Lutron(
                    user_input[CONF_HOST],
                    user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                    user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD),
                )
                
                await self.hass.async_add_executor_job(lutron.connect)
                await self.hass.async_add_executor_job(lutron.load_xml_db)

                # Create entry
                return self.async_create_entry(
                    title=user_input[CONF_HOST],
                    data=user_input,
                )
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                    vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
                }
            ),
            errors=errors,
        ) 