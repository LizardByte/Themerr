"""Credentialed Jellyfin REST access with fixed routes and opaque UUIDs."""

# standard imports
from urllib.parse import quote, urlsplit
from uuid import UUID

# lib imports
import requests

# local imports
from common import logger
from media_servers.base import MediaServerError


log = logger.get_logger(__name__)


def identifier(value: str) -> str:
    """Normalize a native Jellyfin UUID before constructing an API route.

    Parameters
    ----------
    value : str
        Native item, library, or server identifier.

    Returns
    -------
    str
        Hexadecimal UUID without separators.
    """
    try:
        return UUID(str(value)).hex
    except (
        ValueError,
        TypeError,
        AttributeError,
    ) as exc:
        raise MediaServerError('Invalid Jellyfin identifier.', 404) from exc


def base_url(value: str) -> str:
    """Validate an administrator-configured HTTP base URL, including a proxy prefix.

    Parameters
    ----------
    value : str
        Server address without credentials, query parameters, or fragments.

    Returns
    -------
    str
        Address without a trailing slash.
    """
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme
            in (
                'http',
                'https',
            )
            and parts.hostname
            and not parts.username
            and not parts.password
            and not parts.query
            and not parts.fragment
            and all(ord(c) > 32 and c not in '\\%' for c in value)
            and len(value) <= 2048
            and not any(
                segment
                in (
                    '.',
                    '..',
                )
                for segment in parts.path.split('/')
            )
        )
        if not valid or (parts.port is not None and not 1 <= parts.port <= 65535):
            raise ValueError
    except (
        ValueError,
        TypeError,
        AttributeError,
    ) as exc:
        raise MediaServerError('Enter a valid HTTP or HTTPS server address.', 400) from exc
    return value.rstrip('/')


class Client:
    """Keep a saved Jellyfin API key in backend-only request headers.

    Parameters
    ----------
    url : str
        Validated administrator-configured server URL.
    token : str
        Jellyfin administrator API key.
    """

    def __init__(self, url: str, token: str):
        self.url = base_url(url)
        self.token = token
        self.server_version = None

    def request(self, method, path, *, error_messages=None, **kwargs):
        """Request a code-owned route and translate failures to fixed client messages.

        Parameters
        ----------
        method : str
            HTTP method for the Jellyfin operation.
        path : str
            Code-owned API route, containing only validated identifiers.
        error_messages : dict of int to str, optional
            Code-owned explanations for statuses with a known meaning on this route.
            Upstream error bodies are never exposed.
        **kwargs : object
            Request options forwarded to Requests.

        Returns
        -------
        requests.Response
            Successful response that the caller must close.

        Raises
        ------
        MediaServerError
            Jellyfin could not be reached or rejected the request.
        """
        headers = {
            'Authorization': f'MediaBrowser Token="{quote(self.token, safe="")}"',
            **kwargs.pop('headers', {}),
        }
        try:
            # CodeQL py/full-ssrf: connecting to an administrator-selected media server is intentional.
            # Setup requires an authenticated admin session and CSRF; base_url restricts schemes and
            # rejects userinfo/queries. LAN and loopback addresses are required; redirects are disabled.
            response = requests.request(
                method,
                f'{self.url}{path}',
                headers=headers,
                allow_redirects=False,
                timeout=kwargs.pop('timeout', 30),
                **kwargs,
            )
        except requests.RequestException as exc:
            log.warning('Jellyfin %s %s failed with %s.', method, path, type(exc).__name__)
            raise MediaServerError('Could not reach Jellyfin. Check its address and API key.', 502) from exc
        if not 200 <= response.status_code < 300:
            status = response.status_code
            response.close()
            log.warning('Jellyfin %s %s failed with HTTP %s.', method, path, status)
            message = {
                401: 'Jellyfin rejected the API key.',
                403: 'An administrator Jellyfin API key is required.',
                404: 'This Jellyfin resource is unavailable.',
                409: 'Jellyfin protected an existing theme or rejected the connector version.',
                **(error_messages or {}),
            }.get(
                status,
                f'Jellyfin could not complete this request (HTTP {status}). '
                'Check the Jellyfin server log for details.',
            )
            raise MediaServerError(
                message,
                status
                if status
                in (
                    401,
                    403,
                    404,
                    409,
                )
                else 502,
            )
        return response

    def json(self, method, path, **kwargs):
        """Read JSON without retaining upstream connections or error bodies."""
        # Jellyfin 12 defaults to camelCase; pin the documented profile used by both ABIs.
        kwargs['headers'] = {
            'Accept': 'application/json; profile="PascalCase"',
            **kwargs.pop('headers', {}),
        }
        response = self.request(method, path, **kwargs)
        try:
            data = response.json()
            if path == '/System/Info' and isinstance(data, dict):
                self.server_version = data.get('Version')
            return data
        except ValueError as exc:
            log.warning('Jellyfin %s %s returned invalid JSON.', method, path)
            raise MediaServerError('Jellyfin returned an invalid response.', 502) from exc
        finally:
            response.close()
