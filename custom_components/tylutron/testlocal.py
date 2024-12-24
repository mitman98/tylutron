#!/usr/bin/env python3

import asyncio
import logging
import sys
from typing import Optional

from lutronlib.lutronlib import (
    Lutron,
    ThermostatMode,
    ThermostatFanMode,
    Thermostat,
    LutronEvent,
)

# Set up logging
logging.basicConfig(level=logging.DEBUG)
_LOGGER = logging.getLogger(__name__)

class ThermostatTester:
    def __init__(self, host: str, username: str, password: str):
        self.lutron = Lutron(host, username, password)
        self.thermostat: Optional[Thermostat] = None
        self.event_received = asyncio.Event()
        self.last_event = None
        self.last_params = None
        self.running = True
        self.monitor_task = None

    def handle_update(self, device, context, event, params):
        """Handle updates from the thermostat."""
        print(f"\n>>> ASYNC UPDATE from {device.name}: event={event}, params={params}")
        self.last_event = event
        self.last_params = params
        self.event_received.set()

    async def monitor_updates(self):
        """Background task to monitor for updates."""
        _LOGGER.info("Started monitoring for async updates...")
        while self.running:
            await asyncio.sleep(0.1)  # Small sleep to prevent CPU hogging

    async def connect(self):
        """Connect to the Lutron system."""
        try:
            # Connect and load the database
            self.lutron.connect()
            
            # Use cached XML if available
            cache_path = "lutron_db.xml"
            self.lutron.load_xml_db(cache_path=cache_path)
            
            # Find the first thermostat
            for area in self.lutron.areas:
                if area.thermostats:
                    self.thermostat = area.thermostats[0]
                    break
            
            if not self.thermostat:
                _LOGGER.error("No thermostats found!")
                return False

            _LOGGER.info(f"Found thermostat: {self.thermostat.name}")
            
            # Subscribe to updates
            self.thermostat.subscribe(self.handle_update, None)

            # Start monitoring task only once
            if not self.monitor_task:
                self.monitor_task = asyncio.create_task(self.monitor_updates())
            return True

        except Exception as e:
            _LOGGER.exception("Error connecting to Lutron system")
            return False

    async def wait_for_event(self, timeout: float = 5.0, expected_event=None):
        """Wait for an event with timeout."""
        try:
            start_time = asyncio.get_event_loop().time()
            while True:
                await asyncio.wait_for(self.event_received.wait(), timeout=timeout)
                self.event_received.clear()
                                
                # Calculate remaining timeout
                elapsed = asyncio.get_event_loop().time() - start_time
                timeout = max(0, timeout - elapsed)
                if timeout <= 0:
                    _LOGGER.warning(f"Timeout waiting for {expected_event}, got {self.last_event} instead")
                    return False
                
                _LOGGER.debug(f"Received {self.last_event}, waiting for {expected_event}")
                
        except asyncio.TimeoutError:
            _LOGGER.warning(f"Timeout waiting for event {expected_event}")
            return False

    async def test_get_values(self):
        """Test getting current values."""
        print("\nCurrent Values:")
        print(f"Temperature: {self.thermostat.temperature}°F")
        print(f"Mode: {self.thermostat.mode}")
        print(f"Fan Mode: {self.thermostat.fan_mode}")
        print(f"Heat Setpoint: {self.thermostat.heat_setpoint}°F")
        print(f"Cool Setpoint: {self.thermostat.cool_setpoint}°F")
        print(f"Eco Mode: {self.thermostat.eco_mode}")

    async def test_mode_change(self):
        """Test changing the HVAC mode."""
        print("\nTesting mode change...")
        original_mode = self.thermostat.mode
        
        # Test setting to HEAT
        print("Setting mode to HEAT")
        # Query current mode first
        self.lutron.send(
            Lutron.OP_QUERY,  # '?'
            'HVAC',
            self.thermostat.id,
            3  # Action for mode
        )
        await asyncio.sleep(1)
        
        # Then send mode change
        self.thermostat.set_mode(ThermostatMode.HEAT)
        if await self.wait_for_event(timeout=10.0, expected_event=Thermostat.Event.MODE_CHANGED):
            print(f"Mode change confirmed: {self.last_params}")
        else:
            print("Failed to get mode change confirmation")
        
        await asyncio.sleep(5)
        
        # Test setting back to original mode
        print(f"Setting mode back to {original_mode}")
        self.lutron.send(
            Lutron.OP_EXECUTE,
            'HVAC',
            self.thermostat.id,
            3,
            original_mode.value
        )
        if await self.wait_for_event(timeout=10.0, expected_event=Thermostat.Event.MODE_CHANGED):
            print(f"Mode change confirmed: {self.last_params}")
        else:
            print("Failed to get mode change confirmation")

    async def test_fan_mode(self):
        """Test changing the fan mode."""
        print("\nTesting fan mode change...")
        original_mode = self.thermostat.fan_mode
        
        # Test setting to ON
        print("Setting fan to ON")
        self.thermostat.set_fan_mode(ThermostatFanMode.ON)
        if await self.wait_for_event():
            print(f"Fan mode change confirmed: {self.last_params}")
        
        await asyncio.sleep(2)
        
        # Test setting back to original mode
        print(f"Setting fan back to {original_mode}")
        self.thermostat.set_fan_mode(original_mode)
        if await self.wait_for_event():
            print(f"Fan mode change confirmed: {self.last_params}")

    async def test_setpoints(self):
        """Test changing temperature setpoints."""
        print("\nTesting setpoint changes...")
        original_heat = self.thermostat.heat_setpoint
        original_cool = self.thermostat.cool_setpoint
        
        # Test setting new setpoints
        new_heat = original_heat + 1
        new_cool = original_cool + 1
        
        print(f"Setting setpoints to Heat:{new_heat}°F Cool:{new_cool}°F")
        self.thermostat.set_setpoints(new_heat, new_cool)
        if await self.wait_for_event():
            print(f"Setpoint change confirmed: {self.last_params}")
        
        await asyncio.sleep(2)
        
        # Test setting back to original setpoints
        print(f"Setting setpoints back to Heat:{original_heat}°F Cool:{original_cool}°F")
        self.thermostat.set_setpoints(original_heat, original_cool)
        if await self.wait_for_event():
            print(f"Setpoint change confirmed: {self.last_params}")

    async def test_eco_mode(self):
        """Test eco mode."""
        print("\nTesting eco mode...")
        original_eco = self.thermostat.eco_mode
        
        # Toggle eco mode
        new_eco = not original_eco
        print(f"Setting eco mode to: {new_eco}")
        self.thermostat.set_eco_mode(new_eco)
        if await self.wait_for_event():
            print(f"Eco mode change confirmed: {self.last_params}")
        
        await asyncio.sleep(2)
        
        # Set back to original
        print(f"Setting eco mode back to: {original_eco}")
        self.thermostat.set_eco_mode(original_eco)
        if await self.wait_for_event():
            print(f"Eco mode change confirmed: {self.last_params}")

    async def run_tests(self):
        """Run all tests."""
        if not self.thermostat:
            _LOGGER.error("No thermostat available")
            return

        # First get current values
        await self.test_get_values()

        # Run interactive tests
        while True:
            print("\nAvailable tests:")
            print("1. Get current values")
            print("2. Test mode change")
            print("3. Test fan mode")
            print("4. Test setpoints")
            print("5. Test eco mode")
            print("q. Quit")
            
            # Use asyncio.get_event_loop().run_in_executor for input
            loop = asyncio.get_event_loop()
            choice = await loop.run_in_executor(None, input, "\nSelect test (1-5, q to quit): ")
            
            if choice == 'q':
                self.running = False  # Stop the monitor task
                if self.monitor_task:
                    await self.monitor_task  # Wait for monitor task to finish
                break
            elif choice == '1':
                await self.test_get_values()
            elif choice == '2':
                await self.test_mode_change()
            elif choice == '3':
                await self.test_fan_mode()
            elif choice == '4':
                await self.test_setpoints()
            elif choice == '5':
                await self.test_eco_mode()

async def main():
    host = '192.168.1.160'
    username = 'lutron'
    password = 'integration'

    tester = ThermostatTester(host, username, password)
    if await tester.connect():
        await tester.run_tests()
    else:
        _LOGGER.error("Failed to connect")

if __name__ == "__main__":
    asyncio.run(main()) 