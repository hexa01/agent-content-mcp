"""HTTP client for a django-agent-content API: authentication, the HTTPS rule and readable errors."""

from urllib.parse import urljoin, urlsplit

import httpx

LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1'}


class APIError(Exception):
    """A request the site refused or couldn't answer; the message is written for the agent."""


class ContentAPI:
    def __init__(self, base_url: str, token: str, timeout: float = 20):
        if not base_url.endswith('/'):
            base_url += '/'
        parts = urlsplit(base_url)
        # the token travels in a header, so anything that isn't this machine must be HTTPS
        if parts.scheme != 'https' and parts.hostname not in LOCAL_HOSTS:
            raise SystemExit(f'agent-content-mcp: AGENT_CONTENT_URL must use https:// (got {base_url}). Plain http is only allowed for localhost.')
        self.base_url = base_url
        self.client = httpx.AsyncClient(
            headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
            timeout=timeout, follow_redirects=False,
        )

    async def request(self, method: str, path: str, json: dict | None = None, params: dict | None = None):
        url = urljoin(self.base_url, path.lstrip('/'))
        try:
            response = await self.client.request(method, url, json=json, params=params)
        except httpx.HTTPError as error:
            raise APIError(f'Could not reach the site at {self.base_url}: {error}') from error
        if response.is_success:
            return response.json() if response.content else {}
        raise APIError(self._describe(response))

    @staticmethod
    def _describe(response: httpx.Response) -> str:
        """The API's own message (and field errors) when it sends one, so the agent can fix its request."""
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = body.get('error') or response.reason_phrase or 'Request failed'
        if fields := body.get('fields'):
            message += ' ' + _flatten(fields)
        hints = {401: ' Check AGENT_CONTENT_KEY (expired or revoked?).', 403: ' The key\'s user lacks permission for this.'}
        return f'{response.status_code}: {message}{hints.get(response.status_code, "")}'

    async def version(self) -> int | None:
        body = await self.request('GET', '')
        return body.get('version')


def _flatten(errors, prefix=''):
    """Field errors as one line, e.g. "slug: This field is required.; items[0].why: This field is required."."""
    parts = []
    for name, value in errors.items():
        key = f'{prefix}{name}'
        if isinstance(value, dict):
            parts.append(_flatten(value, key + '.'))
        else:
            parts.append(f'{key}: {" ".join(value) if isinstance(value, list) else value}')
    return '; '.join(parts)
