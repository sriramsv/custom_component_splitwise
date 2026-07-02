"""Application credentials platform for Splitwise."""

from homeassistant.components.application_credentials import (
    AuthImplementation,
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant

from .const import OAUTH2_AUTHORIZE, OAUTH2_TOKEN

# Splitwise's OAuth2 token endpoint does not return an "expires_in" field
# (its tokens don't expire and the API has no refresh flow), but Home
# Assistant's generic OAuth2 implementation requires that field and aborts
# the config flow with "oauth_error" ("Received invalid token data") if it's
# missing. Inject a long synthetic expiry so HA treats the token as valid.
_SYNTHETIC_EXPIRES_IN = 3153600000  # ~100 years


class SplitwiseOAuth2Implementation(AuthImplementation):
    """OAuth2 implementation that tolerates Splitwise's non-expiring tokens."""

    async def _token_request(self, data: dict) -> dict:
        token = await super()._token_request(data)
        token.setdefault("expires_in", _SYNTHETIC_EXPIRES_IN)
        return token


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return authorization server for Splitwise."""
    return AuthorizationServer(authorize_url=OAUTH2_AUTHORIZE, token_url=OAUTH2_TOKEN)


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> SplitwiseOAuth2Implementation:
    """Return a custom auth implementation that tolerates missing expires_in."""
    authorization_server = await async_get_authorization_server(hass)
    return SplitwiseOAuth2Implementation(
        hass, auth_domain, credential, authorization_server
    )
