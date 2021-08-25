"""Platform for sensor integration."""

from homeassistant.const import CURRENCY_DOLLAR
from homeassistant.helpers.entity import Entity
import homeassistant.helpers.config_validation as cv
from homeassistant.components import http
from homeassistant.helpers import network
from homeassistant.core import callback
import voluptuous as vol
from .const import DOMAIN
from aiohttp import web
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import CONF_CLIENT_SECRET, CONF_CLIENT_ID
from splitwise import Splitwise
import logging, os, json

_TOKEN_FILE = "splitwise.conf"
_LOGGER = logging.getLogger(__name__)
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_CLIENT_ID): cv.string,
        vol.Required(CONF_CLIENT_SECRET): cv.string,
    }
)
DATA_CALLBACK = "splitwise-callback"
AUTH_CALLBACK_PATH = "/api/splitwise/callback"
REDIRECT_URI = "http://localhost:8123" + AUTH_CALLBACK_PATH


def setup(hass, config):
    """Your controller/hub specific code."""
    # Data that you want to share with your platforms

    hass.helpers.discovery.load_platform("sensor", DOMAIN, {}, config)

    return True


def setup_platform(hass, config, add_entities, discovery_info=None):
    client_id = config[CONF_CLIENT_ID]
    client_secret = config[CONF_CLIENT_SECRET]
    add_entities([SplitwiseSensor(hass, client_id, client_secret)])


class SplitwiseApi:
    def __init__(self, sensor, client_id, client_secret):
        self.sensor = sensor
        self.secret = None
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = None
        try:
            self.splitwise = Splitwise(
                consumer_key=client_id, consumer_secret=client_secret
            )
        except Exception as e:
            raise Exception("Cannot initialize Splitwise Client:{}".format(e))

    @property
    def token_file_name(self):
        # From config/custom_components/splitwise to config/
        base_path = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        token_file = _TOKEN_FILE
        full_token_filepath = os.path.join(base_path, token_file)
        return full_token_filepath

    def get_access_token_from_file(self):
        if not os.path.isfile(self.token_file_name):
            self.get_credentials()
            return

        with open(self.token_file_name, "r") as token_file:
            token_data = json.loads(token_file.read()) or {}

        access_token = token_data.get("access_token")
        if not access_token:
            code = token_data.get("code")
            if code:
                self.get_access_token(code)
        else:
            self.splitwise.setOAuth2AccessToken(access_token)

    def get_access_token(self, code):
        with open(self.token_file_name, "r+") as token_file:
            token_data = json.loads(token_file.read()) or {}
        access_token = token_data.get("access_token")
        if not access_token:
            access_token = self.splitwise.getOAuth2AccessToken(code, REDIRECT_URI)
        _LOGGER.debug("Access_token:{}".format(access_token))
        with open(self.token_file_name, "w+") as token_file:
            token_file.write(json.dumps({"access_token": access_token}))
        self.splitwise.setAccessToken(access_token)

    def get_credentials(self):
        url, state = self.splitwise.getOAuth2AuthorizeURL(REDIRECT_URI)
        _LOGGER.debug(url)
        self.sensor.create_oauth_view(url)


class SplitwiseSensor(Entity):
    def __init__(self, hass, client_id, client_secret):
        """Initialize the sensor."""
        self.hass = hass
        self.api = SplitwiseApi(self, client_id, client_secret)
        self._state = None
        self._user_id = None
        self.currency = None
        self._first_name = None
        self._last_name = None
        self._friends_list = {}
        self._group_map = {}
        self._id_map = {}
        self.api.get_access_token_from_file()

    @property
    def name(self):
        """Return the name of the sensor."""
        return "Splitwise Sensor"

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return self.currency

    @property
    def extra_state_attributes(self):
        m = {}
        if self._user_id:
            m["id"] = self._user_id
        if len(self._friends_list) > 0:
            for k, v in self._friends_list.items():
                m["friend_{}".format(k)] = v["total_balance"]
        if self._first_name:
            m["first_name"] = self._first_name
        if self._last_name:
            m["last_name"] = self._last_name
        if len(self._group_map) > 0:
            for k, v in self._group_map.items():
                m["group_{}".format(k)] = v
        return m

    def update(self):
        """Fetch new state data for the sensor.

        This is the only method that should fetch new data for Home Assistant.
        """
        self.api.get_access_token_from_file()
        user = self.api.splitwise.getCurrentUser()
        self._user_id = user.getId()
        self.currency = user.getDefaultCurrency()
        self._first_name = user.getFirstName().title().lower()
        self._id_map[self._user_id] = self._first_name
        self._last_name = user.getLastName().title().lower()
        friends = self.api.splitwise.getFriends()
        all_balance = 0.0
        for f in friends:
            name = f.getFirstName().title().lower()
            id = f.getId()
            total_balance = 0.0
            for balance in f.getBalances():
                amount = float(balance.getAmount())
                total_balance += amount
            self._friends_list[name] = {
                "total_balance": total_balance,
                "id": id,
            }
            self._id_map[id] = name
            all_balance += total_balance

        groups = self.api.splitwise.getGroups()
        groupMap = {}
        for g in groups:
            groupDebts = 0.0
            groupName = (
                g.getName()
                .lower()
                .replace(" ", "_", -1)
                .strip("_")
                .replace("'", "_", -1)
            )
            debts = g.getOriginalDebts()
            for d in debts:
                amount = d.getAmount()
                trans = "{}->{}".format(
                    self._id_map.get(d.getFromUser()), self._id_map.get(d.getToUser())
                )
                _LOGGER.debug(trans)
                groupDebts += float(amount)
            groupMap[groupName] = groupDebts
        self._state = all_balance
        self._group_map = groupMap

    def create_oauth_view(self, auth_url):
        try:
            self.hass.http.register_view(
                SplitwiseAuthCallbackView(self, self.api.token_file_name)
            )
        except Exception as e:
            _LOGGER.error("Splitwise CallbackView Error {}".format(e))
            return

        self.hass.components.persistent_notification.create(auth_url, DOMAIN, "foo")


class SplitwiseAuthCallbackView(http.HomeAssistantView):
    """Web view that handles OAuth authentication and redirection flow."""

    requires_auth = False
    url = AUTH_CALLBACK_PATH
    name = "api:splitwise:callback"

    def __init__(self, sensor, token_file):
        self.sensor = sensor
        self.token_file_name = token_file

    @callback
    async def get(self, request):  # pylint: disable=no-self-use
        """Handle browser HTTP request."""
        hass = request.app["hass"]
        params = request.query
        response = self.json_message("You can close this window now")

        code = params.get("code")
        code_data = {"code": code}
        with open(self.token_file_name, "w+") as token_file:
            token_file.write(json.dumps(code_data))
        await hass.async_add_executor_job(self.sensor.update)
        return response
