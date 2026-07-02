"""Platform for sensor integration."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SplitwiseBalanceEntry, SplitwiseDataUpdateCoordinator

SENSOR_NAME = "Splitwise"


def _entry_to_dict(entry: SplitwiseBalanceEntry, *, magnitude: bool = False) -> dict:
    # The default-currency "balance" is shown as a positive magnitude to match
    # the sensor's own positive native_value. "other_currencies" (a
    # supplementary breakdown for currencies other than the account default)
    # keeps its original sign, since it isn't necessarily the same direction.
    balance = abs(entry.balance) if magnitude else entry.balance
    d = {"name": entry.name, "balance": balance}
    if entry.id is not None:
        d["id"] = entry.id
    if entry.balances_by_currency:
        d["other_currencies"] = entry.balances_by_currency
    return d


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Splitwise sensors from a config entry."""
    coordinator: SplitwiseDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            SplitwiseYouOweSensor(coordinator, entry),
            SplitwiseYouAreOwedSensor(coordinator, entry),
        ]
    )


class SplitwiseBaseSensor(
    CoordinatorEntity[SplitwiseDataUpdateCoordinator], SensorEntity
):
    _attr_has_entity_name = True
    _attr_icon = "mdi:cash"

    def __init__(
        self,
        coordinator: SplitwiseDataUpdateCoordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=SENSOR_NAME,
            manufacturer="Splitwise",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_unit_of_measurement(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.currency

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        if data is None:
            return {}
        return {
            "id": data.user_id,
            "first_name": data.first_name,
            "last_name": data.last_name,
        }


class SplitwiseYouOweSensor(SplitwiseBaseSensor):
    def __init__(
        self, coordinator: SplitwiseDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, "you_owe", "You Owe")

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.you_owe

    @property
    def extra_state_attributes(self):
        attrs = super().extra_state_attributes
        data = self.coordinator.data
        if data is not None:
            attrs["friends"] = [
                _entry_to_dict(f, magnitude=True) for f in data.friends if f.balance < 0
            ]
            attrs["groups"] = [
                _entry_to_dict(g, magnitude=True) for g in data.groups if g.balance < 0
            ]
        return attrs


class SplitwiseYouAreOwedSensor(SplitwiseBaseSensor):
    def __init__(
        self, coordinator: SplitwiseDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, "you_are_owed", "You Are Owed")

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.you_are_owed

    @property
    def extra_state_attributes(self):
        attrs = super().extra_state_attributes
        data = self.coordinator.data
        if data is not None:
            attrs["friends"] = [
                _entry_to_dict(f, magnitude=True) for f in data.friends if f.balance > 0
            ]
            attrs["groups"] = [
                _entry_to_dict(g, magnitude=True) for g in data.groups if g.balance > 0
            ]
        return attrs
