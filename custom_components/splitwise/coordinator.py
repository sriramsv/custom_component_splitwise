"""Data update coordinator for Splitwise."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from splitwise import Splitwise
from splitwise.exception import SplitwiseException

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=30)


def _sum_by_currency(amounts):
    """Sum a list of (currency_code, amount) pairs, keyed by currency."""
    totals: dict[str, float] = {}
    for currency_code, amount in amounts:
        totals[currency_code] = totals.get(currency_code, 0.0) + amount
    return totals


@dataclass
class SplitwiseBalanceEntry:
    """A friend's or group's balance, in the account's default currency."""

    name: str
    balance: float
    balances_by_currency: dict[str, float]
    id: int | None = None


@dataclass
class SplitwiseData:
    """Aggregated Splitwise data for a single account."""

    user_id: int
    first_name: str
    last_name: str
    currency: str
    you_owe: float
    you_are_owed: float
    friends: list[SplitwiseBalanceEntry] = field(default_factory=list)
    groups: list[SplitwiseBalanceEntry] = field(default_factory=list)


class SplitwiseDataUpdateCoordinator(DataUpdateCoordinator[SplitwiseData]):
    """Fetches and aggregates Splitwise data once per interval for all sensors."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: config_entry_oauth2_flow.OAuth2Session,
        client: Splitwise,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Splitwise",
            update_interval=SCAN_INTERVAL,
        )
        self.entry = entry
        self.session = session
        self.client = client
        # The splitwise library's getNotifications() ignores its own
        # updated_since/limit params (unimplemented upstream), so every poll
        # re-fetches the same "recent notifications" window. Track which ids
        # we've already fired events for so we don't re-fire the same
        # notification every SCAN_INTERVAL for as long as it stays recent.
        self._seen_notification_ids: set[int] = set()

    def _fetch_splitwise_data(self, token):
        """Run on the executor: sync the token into the client and fetch data."""
        client = self.client
        client.setOAuth2AccessToken(token)

        user = client.getCurrentUser()
        friends = client.getFriends()
        groups = client.getGroups()

        try:
            notifications = client.getNotifications()
        except Exception as err:  # noqa: BLE001 - the splitwise library can raise
            # non-SplitwiseException errors (e.g. KeyError) when a notification's
            # data is missing fields it assumes are always present. Don't let a
            # malformed notification take down the whole update.
            _LOGGER.warning("Failed to fetch Splitwise notifications: %s", err)
            notifications = []

        return user, friends, groups, notifications

    async def _async_update_data(self) -> SplitwiseData:
        try:
            await self.session.async_ensure_token_valid()
            user, friends, groups, notifications = await self.hass.async_add_executor_job(
                self._fetch_splitwise_data, self.session.token
            )
        except SplitwiseException as err:
            raise ConfigEntryAuthFailed(
                f"Splitwise authentication failed: {err}"
            ) from err

        currency = user.getDefaultCurrency()
        first_name = user.getFirstName().title().lower()
        last_name = user.getLastName().title().lower()
        id_map = {user.getId(): first_name}

        you_owe = 0.0
        you_are_owed = 0.0
        friend_entries: list[SplitwiseBalanceEntry] = []

        for f in friends:
            name = f.getFirstName().title().lower()
            friend_id = f.getId()
            id_map[friend_id] = name

            balances_by_currency = _sum_by_currency(
                (b.getCurrencyCode(), float(b.getAmount())) for b in f.getBalances()
            )
            balance = balances_by_currency.get(currency, 0.0)

            if balance < 0:
                you_owe += -balance
            elif balance > 0:
                you_are_owed += balance

            other_currencies = {
                code: amount
                for code, amount in balances_by_currency.items()
                if code != currency and amount != 0.0
            }
            if balance != 0.0 or other_currencies:
                friend_entries.append(
                    SplitwiseBalanceEntry(
                        name=name.strip(),
                        balance=balance,
                        balances_by_currency=other_currencies,
                        id=friend_id,
                    )
                )

        group_entries: list[SplitwiseBalanceEntry] = []

        for g in groups:
            amounts_by_currency = []
            for d in g.getOriginalDebts():
                # currency_code is optional on Debt; treat missing as the
                # account's default currency rather than dropping it.
                currency_code = d.getCurrencyCode() or currency

                if id_map.get(d.getToUser()) == first_name:
                    amounts_by_currency.append((currency_code, -float(d.getAmount())))
                elif id_map.get(d.getFromUser()) == first_name:
                    amounts_by_currency.append((currency_code, float(d.getAmount())))

            balances_by_currency = _sum_by_currency(amounts_by_currency)
            balance = balances_by_currency.get(currency, 0.0)
            other_currencies = {
                code: amount
                for code, amount in balances_by_currency.items()
                if code != currency and amount != 0.0
            }
            if balance != 0.0 or other_currencies:
                group_entries.append(
                    SplitwiseBalanceEntry(
                        name=g.getName().strip(),
                        balance=balance,
                        balances_by_currency=other_currencies,
                    )
                )

        self._emit_notifications(notifications)

        if self.entry.unique_id is None:
            self.hass.config_entries.async_update_entry(
                self.entry, unique_id=str(user.getId())
            )

        return SplitwiseData(
            user_id=user.getId(),
            first_name=first_name,
            last_name=last_name,
            currency=currency,
            you_owe=you_owe,
            you_are_owed=you_are_owed,
            friends=friend_entries,
            groups=group_entries,
        )

    def _emit_notifications(self, notifications):
        current_ids = {n.getId() for n in notifications}
        new_notifications = [
            n for n in notifications if n.getId() not in self._seen_notification_ids
        ]

        for n in new_notifications:
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

        self._seen_notification_ids = current_ids
