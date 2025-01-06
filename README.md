# Tylutron

Home Assistant integration for Lutron RadioRA 2 thermostats over the TCP/IP Lutron protocol.

NOTE: This is a completely unsupported fork, but I wanted to make available to others. Use at your own risk. I made this for myself beacuse none of the Lutron integrations supported thermostats. It is a combined fork of pylutron (https://github.com/thecynic/pylutron) and lutron_custom_component (https://github.com/rzulian/lutron_custom_component), which has not had updates for some time. I explictly set this code to not create any Home Assistant entities other than thermostats and a sensor for each thermostat indicating the lutron remote temperature sensor status. I use this side by side with the out-of-the-box HA Lutron integration for lights, blinds, switches, etc.

## Features
- Full thermostat control (heat, cool, auto modes)
- Fan control
- ECO mode support
- Emergency heat readonly support (when available)
- Temperature monitoring
- Lutron remote temperature sensor status reported as HA sensor
- XML database caching

## Installation

### HACS (Recommended)
1. Open HACS
2. Click the three dots in top right
3. Select "Custom repositories"
4. Add this repository URL
5. Select "Integration" as the category
6. Click Add
7. Install "Tylutron" from HACS

### Manual Installation 
