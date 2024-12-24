"""Platform for Tylutron sensor integration."""
import logging
from typing import Optional

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .lutronlib.lutronlib import (
    Thermostat,
    ThermostatSensorStatus,
)

_LOGGER = logging.getLogger(__name__)

# Map sensor status to human readable strings
SENSOR_STATUS_MAP = {
    ThermostatSensorStatus.ALL_ACTIVE: "Sensors Active",
    ThermostatSensorStatus.MISSING_SENSOR: "Missing Sensor",
    ThermostatSensorStatus.WIRED_ONLY: "Wired Sensor Only",
    ThermostatSensorStatus.NO_SENSOR: "No Sensor",
}

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tylutron sensor platform."""
    lutron = hass.data[DOMAIN][config_entry.entry_id]
    
    entities = []
    for area in lutron.areas:
        for thermostat in area.thermostats:
            entities.append(ThermostatSensorStatusSensor(thermostat))
    
    async_add_entities(entities)

class ThermostatSensorStatusSensor(SensorEntity):
    """Representation of a Tylutron thermostat sensor status."""

    def __init__(self, thermostat: Thermostat):
        """Initialize the sensor."""
        self._thermostat = thermostat
        self._attr_unique_id = f"tylutron_sensor_status_{thermostat.id}"
        self._attr_name = f"{thermostat.name} Sensor Status"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        
        # Add device info
        self._attr_device_info = {
            "identifiers": {(DOMAIN, str(thermostat.id))},
            "name": thermostat.name,
            "manufacturer": "Lutron",
            "model": "Thermostat",
            "via_device": (DOMAIN, thermostat.legacy_uuid or str(thermostat.id)),
        }
        
        # Subscribe to updates
        self._thermostat.subscribe(self._handle_update, None)

    def _handle_update(self, device, context, event, params):
        """Handle updates from the thermostat."""
        if event == Thermostat.Event.SENSOR_STATUS_CHANGED:
            self.schedule_update_ha_state()

    @property
    def native_value(self) -> Optional[str]:
        """Return the state of the sensor."""
        status = self._thermostat.sensor_status
        if status is None:
            return None
        return SENSOR_STATUS_MAP.get(status, "Unknown")

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "raw_status": self._thermostat.sensor_status.value if self._thermostat.sensor_status else None,
        } 