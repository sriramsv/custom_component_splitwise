"""Platform for sensor integration."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from splitwise.exception import SplitwiseException

from . import SplitwiseRuntimeData
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SENSOR_NAME = "Splitwise"
SCAN_INTERVAL = timedelta(minutes=30)


def format_name(str):
    return (
        str.lower()
        .replace(" ", "_")
        .strip("_")
        .replace("'", "_")
        .replace("-", "_")
    )


def _sum_by_currency(amounts):
    """Sum a list of (currency_code, amount) pairs, keyed by currency."""
    totals: dict[str, float] = {}
    for currency_code, amount in amounts:
        totals[currency_code] = totals.get(currency_code, 0.0) + amount
    return totals


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Splitwise sensor from a config entry."""
    runtime_data: SplitwiseRuntimeData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SplitwiseSensor(runtime_data, entry)], update_before_add=True)


class SplitwiseSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Balance"

    def __init__(self, runtime_data: SplitwiseRuntimeData, entry: ConfigEntry) -> None:
        self._runtime = runtime_data
        self._entry = entry

        self._attr_unique_id = f"{entry.entry_id}_balance"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=SENSOR_NAME,
            manufacturer="Splitwise",
            entry_type=DeviceEntryType.SERVICE,
        )

        self._state = None
        self._user_id = None
        self.currency = None
        self._first_name = None
        self._last_name = None

        self._friends_list = {}
        self._group_map = {}
        self._id_map = {}

    @property
    def icon(self):
        return "mdi:cash"

    @property
    def native_value(self):
        return self._state

    @property
    def native_unit_of_measurement(self):
        return self.currency

    @property
    def extra_state_attributes(self):
        m = {}

        if self._user_id:
            m["id"] = self._user_id
        if self._first_name:
            m["first_name"] = self._first_name
        if self._last_name:
            m["last_name"] = self._last_name

        you_owe = 0.0
        you_are_owed = 0.0
        friends = []

        for k, v in self._friends_list.items():
            balance = v["total_balance"]

            if balance != 0.0:
                m[format_name(k)] = balance
            if balance < 0:
                you_owe += -balance
            elif balance > 0:
                you_are_owed += balance

            other_currencies = {
                currency_code: amount
                for currency_code, amount in v["balances_by_currency"].items()
                if currency_code != self.currency and amount != 0.0
            }
            if other_currencies:
                m[f"{format_name(k)}_other_currencies"] = other_currencies

            if balance != 0.0 or other_currencies:
                friend_entry = {"name": k, "id": v["id"], "balance": balance}
                if other_currencies:
                    friend_entry["other_currencies"] = other_currencies
                friends.append(friend_entry)

        groups = []

        for k, v in self._group_map.items():
            balance = v["total_balance"]

            if balance != 0.0:
                m[format_name(k)] = balance

            other_currencies = {
                currency_code: amount
                for currency_code, amount in v["balances_by_currency"].items()
                if currency_code != self.currency and amount != 0.0
            }
            if other_currencies:
                m[f"{format_name(k)}_other_currencies"] = other_currencies

            if balance != 0.0 or other_currencies:
                group_entry = {"name": k, "balance": balance}
                if other_currencies:
                    group_entry["other_currencies"] = other_currencies
                groups.append(group_entry)

        m["you_owe"] = you_owe
        m["you_are_owed"] = you_are_owed
        m["friends"] = friends
        m["groups"] = groups

        return m

    def _fetch_splitwise_data(self, token):
        """Run on the executor: sync the token into the client and fetch data."""
        client = self._runtime.client
        client.setOAuth2AccessToken(token)

        user = client.getCurrentUser()
        friends = client.getFriends()
        groups = client.getGroups()

        try:
            notifications = client.getNotifications()
        except Exception as err:  # noqa: BLE001 - the splitwise library can raise
            # non-SplitwiseException errors (e.g. KeyError) when a notification's
            # data is missing fields it assumes are always present. Don't let a
            # malformed notification take down the whole sensor update.
            _LOGGER.warning("Failed to fetch Splitwise notifications: %s", err)
            notifications = []

        return user, friends, groups, notifications

    async def async_update(self) -> None:
        session = self._runtime.session

        try:
            await session.async_ensure_token_valid()
            user, friends, groups, notifications = await self.hass.async_add_executor_job(
                self._fetch_splitwise_data, session.token
            )
        except SplitwiseException as err:
            raise ConfigEntryAuthFailed(
                f"Splitwise authentication failed: {err}"
            ) from err

        self._user_id = user.getId()
        self.currency = user.getDefaultCurrency()

        self._first_name = user.getFirstName().title().lower()
        self._id_map[self._user_id] = self._first_name

        self._last_name = user.getLastName().title().lower()

        all_balance = 0.0
        for f in friends:
            name = f.getFirstName().title().lower()
            id = f.getId()

            balances_by_currency = _sum_by_currency(
                (b.getCurrencyCode(), float(b.getAmount())) for b in f.getBalances()
            )
            total_balance = balances_by_currency.get(self.currency, 0.0)

            self._friends_list[name] = {
                "total_balance": total_balance,
                "balances_by_currency": balances_by_currency,
                "id": id,
            }

            self._id_map[id] = name
            all_balance += total_balance

        self._state = all_balance
        self._update_group_data(groups)
        self._emit_notifications(notifications)

        if self._entry.unique_id is None:
            self.hass.config_entries.async_update_entry(
                self._entry, unique_id=str(self._user_id)
            )

    def _emit_notifications(self, notifications):
        for n in notifications:
            self.hass.bus.fire(
                "splitwise_notification_event_" + str(n.getType()),
                {
                    "id": n.getId(),
                    "type": n.getType(),
                    "image_url": n.getImageUrl(),
                    "content": n.getContent(),
                    "image_shape": n.getImageShape(),
                    "created_at": n.getCreatedAt(),
                    "created_by": n.getCreatedBy(),
                    "source": {
                        "id": n.source.getId(),
                        "type": n.source.getType(),
                        "url": n.source.getUrl(),
                    },
                },
                origin="REMOTE",
            )

    def _update_group_data(self, groups):
        for g in groups:
            amounts_by_currency = []
            for d in g.getOriginalDebts():
                # currency_code is optional on Debt; treat missing as the
                # account's default currency rather than dropping it.
                currency_code = d.getCurrencyCode() or self.currency

                if self._id_map[d.getToUser()] == self._first_name:
                    amounts_by_currency.append((currency_code, -float(d.getAmount())))
                elif self._id_map[d.getFromUser()] == self._first_name:
                    amounts_by_currency.append((currency_code, float(d.getAmount())))

            balances_by_currency = _sum_by_currency(amounts_by_currency)

            self._group_map[g.getName()] = {
                "total_balance": balances_by_currency.get(self.currency, 0.0),
                "balances_by_currency": balances_by_currency,
            }
