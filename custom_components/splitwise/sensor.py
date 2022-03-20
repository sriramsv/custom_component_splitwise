"""Platform for sensor integration."""

from homeassistant.helpers.entity import Entity
import homeassistant.helpers.config_validation as cv
from homeassistant.components import http
from homeassistant.helpers import network
from homeassistant.core import callback
import voluptuous as vol
from .const import DOMAIN
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import CONF_CLIENT_SECRET, CONF_CLIENT_ID
from splitwise import Splitwise
import logging, os, json
from homeassistant.helpers import network

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
SENSOR_NAME = "Splitwise"


class AuthenticationFailedException(Exception):
    pass


def format_name(str):
    return str.lower().replace(" ", "_").strip("_").replace("'", "_").replace("-", "_")


def get_url(hass):
    """Gets the required Home-Assistant URL for validation.
    Args:
      hass: Hass instance.
    Returns:
      Home-Assistant URL.
    """
    if network:
        try:
            return network.get_url(
                hass,
                allow_external=True,
                allow_internal=True,
                allow_ip=False,
                prefer_external=True,
                require_ssl=True,
            )
        except network.NoURLAvailableError:
            _LOGGER.debug("Hass version does not have get_url helper, using fall back.")

    base_url = hass.config.api.base_url
    if base_url:
        return base_url

    raise ValueError("Unable to obtain HASS url.")


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
        self.isAuthSuccess = False
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

        if "access_token" not in token_data:
            code = token_data.get("code")
            if code:
                self.get_access_token(code)
        else:
            self.isAuthSuccess = True
            self.splitwise.setOAuth2AccessToken(token_data)

    @property
    def is_authenticated(self):
        return self.isAuthSuccess

    def get_access_token(self, code):
        with open(self.token_file_name, "r+") as token_file:
            token_data = json.loads(token_file.read()) or {}
        access_token = token_data.get("access_token")
        if not access_token:
            access_token = self.splitwise.getOAuth2AccessToken(
                code, self.get_redirect_uri()
            )
        with open(self.token_file_name, "w+") as token_file:
            token_file.write(json.dumps(access_token))
        self.isAuthSuccess = True
        self.splitwise.setOAuth2AccessToken(access_token)

    def get_credentials(self):
        url, state = self.splitwise.getOAuth2AuthorizeURL(self.get_redirect_uri())
        self.sensor.create_oauth_view(url)

    def get_redirect_uri(self):
        return "{}{}".format(self.sensor.hass_url, AUTH_CALLBACK_PATH)


class SplitwiseSensor(Entity):
    def __init__(self, hass, client_id, client_secret):
        """Initialize the sensor."""
        self.hass = hass
        self.hass_url = get_url(hass)
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
        if self._first_name:
            m["first_name"] = self._first_name
        if self._last_name:
            m["last_name"] = self._last_name

        if len(self._friends_list) > 0:
            for k, v in self._friends_list.items():
                if v["total_balance"] != 0.0:
                    m[format_name(k)] = v["total_balance"]

        if len(self._group_map) > 0:
            for k, v in self._group_map.items():
                if v != 0.0:
                    m[format_name(k)] = v
        return m

    def update(self):
        """Fetch new state data for the sensor.

        This is the only method that should fetch new data for Home Assistant.
        """
        self.api.get_access_token_from_file()
        if not self.api.is_authenticated:
            raise AuthenticationFailedException("error fetching authentication token")
        self.hass.components.persistent_notification.dismiss(
            notification_id=f"splitwise_setup_{SENSOR_NAME}"
        )
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
            self._state = all_balance
        self.get_group_data()
        self.emit_notifications(self.api.splitwise.getNotifications())

    def emit_notifications(self, notifications):
        for n in notifications:
            self.hass.bus.fire("splitwise_notification_event_" + n.getType(), {
                id: n.getId()
                type: n.getType(),
                image_url: n.getImageUrl(),
                content: n.getContent(),
                image_shape: n.getImageShape(),
                created_at: n.getCreatedAt(),
                created_by: n.getCreatedBy(),
                source: { id: n.source.getId(), type: n.source.getType(), url: n.source.getUrl() }
            }, origin="REMOTE", time_fired=n.getCreatedAt())

    def get_group_data(self):
        groups = self.api.splitwise.getGroups()
        for g in groups:
            amount = 0.0
            for d in g.getOriginalDebts():
                if self._id_map[d.getToUser()] == self._first_name:
                    amount -= float(d.getAmount())
                elif self._id_map[d.getFromUser()] == self._first_name:
                    amount += float(d.getAmount())
            self._group_map[g.getName()] = amount

    def create_oauth_view(self, auth_url):
        try:
            self.hass.http.register_view(
                SplitwiseAuthCallbackView(self, self.api.token_file_name)
            )
        except Exception as e:
            _LOGGER.error("Splitwise CallbackView Error {}".format(e))
            return

        self.hass.components.persistent_notification.create(
            "In order to authorize Home-Assistant to view your Splitwise data, "
            "you must visit: "
            f'<a href="{auth_url}" target="_blank">{auth_url}</a>. Make '
            f"sure that you have added {self.api.redirect_uri} to your "
            "Redirect URIs on Splitwise Developer portal.",
            title=SENSOR_NAME,
            notification_id=f"splitwise_setup_{SENSOR_NAME}",
        )


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
