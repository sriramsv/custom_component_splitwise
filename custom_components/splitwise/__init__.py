"""Splitwise Sensor Custom Component"""

from __future__ import annotations

import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PLATFORM, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    config_entry_oauth2_flow,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.typing import ConfigType
from splitwise import Splitwise

from .const import DOMAIN
from .coordinator import SplitwiseDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Splitwise component, warning about deprecated YAML config."""
    for platform_conf in config.get("sensor", []):
        if platform_conf.get(CONF_PLATFORM) == DOMAIN:
            ir.async_create_issue(
                hass,
                DOMAIN,
                "deprecated_yaml",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="deprecated_yaml",
                learn_more_url="https://github.com/sriramsv/custom_component_splitwise#readme",
            )
            break

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Splitwise from a config entry."""
    _async_remove_stale_entities(hass, entry)

    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    )
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)

    client = Splitwise(
        consumer_key=implementation.client_id,
        consumer_secret=implementation.client_secret,
    )

    coordinator = SplitwiseDataUpdateCoordinator(hass, entry, session, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


def _async_remove_stale_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove entities from unique_ids no longer created by this integration.

    0.3.0 replaced the single "Balance" sensor (unique_id "<entry_id>_balance")
    with separate "You Owe"/"You Are Owed" sensors. Home Assistant doesn't
    remove entities an integration stops creating on its own, so anyone
    upgrading would otherwise be left with a permanently-unavailable orphan.
    """
    ent_reg = er.async_get(hass)
    stale_unique_id = f"{entry.entry_id}_balance"
    if entity_id := ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, stale_unique_id
    ):
        ent_reg.async_remove(entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Splitwise config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unloaded
