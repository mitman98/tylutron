"""Platform for Tylutron climate integration."""
import logging
from typing import Any, Optional

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
    PRESET_ECO,
    PRESET_NONE,
)
from homeassistant.components.climate.const import (
    FAN_ON,
    FAN_OFF,
    FAN_AUTO,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .lutronlib.lutronlib import (
    Lutron,
    ThermostatMode,
    ThermostatFanMode,
    ThermostatCallStatus,
)

_LOGGER = logging.getLogger(__name__)

# Map Lutron modes to Home Assistant modes
MODE_MAP = {
    ThermostatMode.OFF: HVACMode.OFF,
    ThermostatMode.HEAT: HVACMode.HEAT,
    ThermostatMode.COOL: HVACMode.COOL,
    ThermostatMode.AUTO: HVACMode.HEAT_COOL,
    ThermostatMode.EMERGENCY_HEAT: HVACMode.HEAT,
}

# Map Home Assistant modes to Lutron modes
HA_MODE_MAP = {
    HVACMode.OFF: ThermostatMode.OFF,
    HVACMode.HEAT: ThermostatMode.HEAT,
    HVACMode.COOL: ThermostatMode.COOL,
    HVACMode.HEAT_COOL: ThermostatMode.AUTO,
    # Note: EMERGENCY_HEAT is handled separately through is_aux_heat
}

# Map Lutron fan modes to Home Assistant fan modes
FAN_MODE_MAP = {
    ThermostatFanMode.AUTO: FAN_AUTO,
    ThermostatFanMode.ON: FAN_ON,
}

# Map Home Assistant fan modes to Lutron fan modes
HA_FAN_MODE_MAP = {v: k for k, v in FAN_MODE_MAP.items()}

# Add preset mode mapping
PRESET_MAP = {
    True: PRESET_ECO,
    False: PRESET_NONE,
}

# Add reverse mapping
HA_PRESET_MAP = {v: k for k, v in PRESET_MAP.items()}

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tylutron climate platform."""
    lutron = hass.data[DOMAIN][config_entry.entry_id]
    
    entities = []
    for area in lutron.areas:
        for thermostat in area.thermostats:
            entities.append(TylutronThermostat(thermostat))
    
    async_add_entities(entities)

class TylutronThermostat(ClimateEntity):
    """Representation of a Tylutron thermostat."""

    def __init__(self, thermostat):
        """Initialize the thermostat."""
        self._thermostat = thermostat
        self._attr_unique_id = f"tylutron_climate_{thermostat.id}"
        self._attr_name = thermostat.name
        
        _LOGGER.info(
            "Initializing climate entity for thermostat %s (id=%s)",
            thermostat.name, thermostat.id
        )
        
        # Add device info to match sensor
        self._attr_device_info = {
            "identifiers": {(DOMAIN, str(thermostat.id))},
            "name": thermostat.name,
            "manufacturer": "Lutron",
            "model": "Thermostat",
            "via_device": (DOMAIN, thermostat.legacy_uuid or str(thermostat.id)),
        }
        
        # Add PRESET_MODE to supported features
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE |  # For HEAT and COOL modes
            ClimateEntityFeature.TARGET_TEMPERATURE_RANGE |  # For HEAT_COOL mode
            ClimateEntityFeature.FAN_MODE |  # For fan control
            ClimateEntityFeature.TURN_OFF |  # Can turn system off
            ClimateEntityFeature.TURN_ON |  # Can turn system on
            ClimateEntityFeature.PRESET_MODE  # For eco mode
        )
        
        if thermostat.emergency_heat_available:
            self._attr_supported_features |= ClimateEntityFeature.AUX_HEAT

        # Set available modes and fan modes
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]
        self._attr_fan_modes = [FAN_AUTO, FAN_ON]
        
        # Set available presets
        self._attr_preset_modes = [PRESET_NONE, PRESET_ECO]
        
        # Add temperature limits
        self._attr_min_temp = 45  # Typical Lutron min
        self._attr_max_temp = 95  # Typical Lutron max
        
        # Set temperature unit
        self._attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
        
        # Subscribe to updates
        self._thermostat.subscribe(self._handle_update, None)
        _LOGGER.debug("Subscribed to thermostat updates")

    def _handle_update(self, device, context, event, params):
        """Handle updates from the thermostat."""
        _LOGGER.info(
            "Climate entity %s received update - event: %s, params: %s",
            self.name, event, params
        )
        # Add state logging
        _LOGGER.debug(
            "Current state - temp: %s, mode: %s, fan: %s, eco: %s",
            self._thermostat.temperature,
            self._thermostat.mode,
            self._thermostat.fan_mode,
            self._thermostat.eco_mode
        )
        self.schedule_update_ha_state()

    @property
    def current_temperature(self) -> Optional[float]:
        """Return the current temperature."""
        return self._thermostat.temperature

    @property
    def target_temperature(self) -> Optional[float]:
        """Return the temperature we try to reach."""
        if self.hvac_mode == HVACMode.HEAT:
            return self._thermostat.heat_setpoint
        if self.hvac_mode == HVACMode.COOL:
            return self._thermostat.cool_setpoint
        # Don't return a target temp for HEAT_COOL mode - use high/low instead
        return None

    @property
    def target_temperature_high(self) -> Optional[float]:
        """Return the highbound target temperature we try to reach."""
        if self.hvac_mode == HVACMode.HEAT_COOL:
            return self._thermostat.cool_setpoint
        return None

    @property
    def target_temperature_low(self) -> Optional[float]:
        """Return the lowbound target temperature we try to reach."""
        if self.hvac_mode == HVACMode.HEAT_COOL:
            return self._thermostat.heat_setpoint
        return None

    @property
    def hvac_mode(self) -> HVACMode:
        """Return hvac operation ie. heat, cool mode."""
        return MODE_MAP.get(self._thermostat.mode, HVACMode.OFF)

    @property
    def hvac_action(self) -> Optional[HVACAction]:
        """Return the current running hvac operation."""
        if self._thermostat.call_status in (
            ThermostatCallStatus.HEAT_STAGE_1,
            ThermostatCallStatus.HEAT_STAGE_1_2
        ):
            return HVACAction.HEATING
        if self._thermostat.call_status in (
            ThermostatCallStatus.COOL_STAGE_1,
            ThermostatCallStatus.COOL_STAGE_2
        ):
            return HVACAction.COOLING
        if self._thermostat.call_status in (
            ThermostatCallStatus.NONE_LAST_HEAT,
            ThermostatCallStatus.NONE_LAST_COOL
        ):
            return HVACAction.IDLE
        return HVACAction.OFF

    @property
    def fan_mode(self) -> Optional[str]:
        """Return the fan setting."""
        return FAN_MODE_MAP.get(self._thermostat.fan_mode)

    @property
    def preset_mode(self) -> Optional[str]:
        """Return the current preset mode."""
        return PRESET_MAP.get(self._thermostat.eco_mode, PRESET_NONE)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        _LOGGER.debug(
            "Setting temperature for %s with kwargs: %s",
            self.name, kwargs
        )
        try:
            if ATTR_TEMPERATURE in kwargs:
                temp = float(kwargs[ATTR_TEMPERATURE])
                _LOGGER.debug(
                    "Setting single temperature %s for mode %s",
                    temp, self.hvac_mode
                )
                if self.hvac_mode == HVACMode.HEAT:
                    await self.hass.async_add_executor_job(
                        self._thermostat.set_setpoints, temp, None
                    )
                elif self.hvac_mode == HVACMode.COOL:
                    await self.hass.async_add_executor_job(
                        self._thermostat.set_setpoints, None, temp
                    )
            else:
                low_temp = kwargs.get("target_temp_low")
                high_temp = kwargs.get("target_temp_high")
                if low_temp is not None and high_temp is not None:
                    if float(high_temp) <= float(low_temp):
                        raise ValueError("High temp must be greater than low temp")
                    await self.hass.async_add_executor_job(
                        self._thermostat.set_setpoints, float(low_temp), float(high_temp)
                    )
        except ValueError as err:
            _LOGGER.error("Invalid temperature value: %s", err)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode."""
        _LOGGER.debug(
            "Setting fan mode for %s to: %s",
            self.name, fan_mode
        )
        lutron_mode = HA_FAN_MODE_MAP.get(fan_mode)
        _LOGGER.debug("Translated to Lutron mode: %s", lutron_mode)
        if lutron_mode is not None:
            await self.hass.async_add_executor_job(
                self._thermostat.set_fan_mode, lutron_mode
            )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        _LOGGER.debug(
            "Setting HVAC mode for %s to: %s",
            self.name, hvac_mode
        )
        lutron_mode = HA_MODE_MAP.get(hvac_mode)
        _LOGGER.debug("Translated to Lutron mode: %s", lutron_mode)
        if lutron_mode is not None:
            await self.hass.async_add_executor_job(
                self._thermostat.set_mode, lutron_mode
            )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        if preset_mode not in self.preset_modes:
            raise ValueError(f"Invalid preset mode: {preset_mode}")
            
        eco_enabled = HA_PRESET_MAP.get(preset_mode, False)
        await self.hass.async_add_executor_job(
            self._thermostat.set_eco_mode, eco_enabled
        ) 

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return self._attr_min_temp

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return self._attr_max_temp

    @property
    def is_aux_heat(self) -> Optional[bool]:
        """Return true if aux heat is on."""
        return self._thermostat.mode == ThermostatMode.EMERGENCY_HEAT 