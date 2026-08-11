"""One-time interactive OAuth authorization + silent token refresh.

Google's Health API uses standard Google OAuth 2.0 (the old Fitbit Web API's
own auth is being decommissioned alongside it). This runs the "web app"
authorization-code flow locally: open the consent screen in the user's real
browser, catch the redirect on a loopback HTTP server, exchange the code for
a refresh token, and cache it. Every subsequent call just refreshes silently.

Run once with ``fitbit sync-auth``. After that, ``get_access_token()`` is all
any sync code needs.
"""

from __future__ import annotations

import json
import os
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import certifi
import yaml

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# python.org's macOS builds don't wire up the system CA store, so
# urllib's default SSL context fails cert verification on some machines.
# certifi ships a CA bundle directly, sidestepping that regardless of how
# the interpreter was installed.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

DEFAULT_SECRETS_PATH = Path("secrets.local.yaml")
DEFAULT_TOKEN_CACHE_PATH = Path(".google_health_token.json")


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: list[str]


def load_oauth_client(path: Path = DEFAULT_SECRETS_PATH) -> OAuthClient:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Create it with a `google_health:` block containing "
            "client_id, client_secret, redirect_uri, and scopes."
        )
    raw = yaml.safe_load(path.read_text()) or {}
    gh = raw.get("google_health")
    if not gh:
        raise ValueError(f"{path} has no `google_health:` block.")
    return OAuthClient(
        client_id=gh["client_id"],
        client_secret=gh["client_secret"],
        redirect_uri=gh.get("redirect_uri", "http://localhost:8765/callback"),
        scopes=gh["scopes"],
    )


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures ?code=...&state=... on the OAuth redirect.

    Some browsers fire a stray GET /favicon.ico right after following the
    redirect. Only a request whose path matches the callback path is treated
    as the real one; anything else gets a bare 204 and the server keeps
    waiting.
    """

    result: dict[str, str] | None = None
    callback_path: str = "/callback"

    def do_GET(self) -> None:  # noqa: N802 (http.server's naming convention)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != _CallbackHandler.callback_path:
            self.send_response(204)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        body = (
            "<html><body><h3>fitbit-analytics: authorized.</h3>"
            "<p>You can close this tab and go back to the terminal.</p>"
            "</body></html>"
        )
        self.wfile.write(body.encode())

    def log_message(self, fmt: str, *args) -> None:  # silence default request logging
        pass


def _await_redirect(port: int, callback_path: str) -> dict[str, str]:
    _CallbackHandler.result = None
    _CallbackHandler.callback_path = callback_path
    server = HTTPServer(("localhost", port), _CallbackHandler)
    while _CallbackHandler.result is None:
        server.handle_request()
    server.server_close()
    return _CallbackHandler.result


def authorize(client: OAuthClient, token_cache_path: Path = DEFAULT_TOKEN_CACHE_PATH) -> None:
    """Run the interactive flow once and cache the resulting refresh token."""
    redirect_parts = urllib.parse.urlparse(client.redirect_uri)
    port = redirect_parts.port or 8765
    callback_path = redirect_parts.path or "/callback"
    state = secrets.token_urlsafe(16)

    query = urllib.parse.urlencode(
        {
            "client_id": client.client_id,
            "redirect_uri": client.redirect_uri,
            "response_type": "code",
            "scope": " ".join(client.scopes),
            "access_type": "offline",
            "prompt": "consent",  # forces a refresh_token even on repeat auth
            "state": state,
        }
    )
    auth_url = f"{AUTH_ENDPOINT}?{query}"

    print(f"Opening browser for authorization:\n  {auth_url}\n")
    print(f"Waiting for redirect on {client.redirect_uri} ...")
    webbrowser.open(auth_url)

    result = _await_redirect(port, callback_path)

    if result.get("state") != state:
        raise RuntimeError("OAuth state mismatch -- possible CSRF, aborting.")
    if "error" in result:
        raise RuntimeError(f"Authorization denied: {result['error']}")
    code = result.get("code")
    if not code:
        raise RuntimeError(f"No authorization code in redirect: {result}")

    tokens = _exchange_code(client, code)
    if "refresh_token" not in tokens:
        raise RuntimeError(
            "No refresh_token in response. Google only issues one on first consent "
            "(or with prompt=consent, which this always sets) -- check the Google "
            "Cloud project's OAuth client is correct."
        )

    _save_tokens(token_cache_path, tokens)
    print(f"Authorized. Token cached at {token_cache_path}")


def _exchange_code(client: OAuthClient, code: str) -> dict:
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "redirect_uri": client.redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode()
    return _post_token(data)


def _refresh(client: OAuthClient, refresh_token: str) -> dict:
    data = urllib.parse.urlencode(
        {
            "refresh_token": refresh_token,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "grant_type": "refresh_token",
        }
    ).encode()
    return _post_token(data)


def _post_token(data: bytes) -> dict:
    req = urllib.request.Request(TOKEN_ENDPOINT, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, context=_SSL_CONTEXT) as resp:  # noqa: S310
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if "invalid_grant" in body:
            # The expected weekly failure mode for a Testing-status OAuth
            # app (7-day refresh token, see CLAUDE.md) -- make the fix
            # obvious instead of surfacing a bare HTTP 400 traceback.
            raise RuntimeError(
                "Refresh token expired or revoked (invalid_grant) -- this is "
                "the normal 7-day Testing-mode limit, not a bug. Run "
                "`fitbit sync-auth` to re-authorize."
            ) from e
        raise RuntimeError(f"Token endpoint returned {e.code}: {body[:300]}") from e


def _save_tokens(path: Path, tokens: dict) -> None:
    cached = dict(tokens)
    cached["obtained_at"] = time.time()
    path.write_text(json.dumps(cached, indent=2))


_ENV_CLIENT_ID = "GOOGLE_HEALTH_CLIENT_ID"
_ENV_CLIENT_SECRET = "GOOGLE_HEALTH_CLIENT_SECRET"
_ENV_REFRESH_TOKEN = "GOOGLE_HEALTH_REFRESH_TOKEN"


def get_access_token(
    secrets_path: Path = DEFAULT_SECRETS_PATH,
    token_cache_path: Path = DEFAULT_TOKEN_CACHE_PATH,
) -> str:
    """Return a valid access token, refreshing silently if the cached one expired.

    Two paths, tried in this order:

    1. CI / env-var: if GOOGLE_HEALTH_CLIENT_ID/SECRET/REFRESH_TOKEN are all
       set, refresh directly from those -- no local files needed at all.
       This is the GitHub Actions path; it never touches disk, so there's
       nothing to gitignore and nothing that survives between runner
       instances by accident.
    2. Local dev: the token cache file written by `fitbit sync-auth`,
       refreshed in place when it's within 60s of expiring.

    Both paths raise on a refresh failure rather than swallowing it -- an
    expired 7-day Testing-mode refresh token (see CLAUDE.md) must fail the
    calling job loudly, not silently skip a sync.
    """
    if os.environ.get(_ENV_CLIENT_ID) and os.environ.get(_ENV_REFRESH_TOKEN):
        client = OAuthClient(
            client_id=os.environ[_ENV_CLIENT_ID],
            client_secret=os.environ.get(_ENV_CLIENT_SECRET, ""),
            redirect_uri="",  # unused for a refresh-token grant
            scopes=[],
        )
        refreshed = _refresh(client, os.environ[_ENV_REFRESH_TOKEN])
        return refreshed["access_token"]

    if not token_cache_path.exists():
        raise FileNotFoundError(
            f"No token cache at {token_cache_path}. Run `fitbit sync-auth` first."
        )
    cached = json.loads(token_cache_path.read_text())
    expires_at = cached["obtained_at"] + cached.get("expires_in", 3600) - 60  # 60s safety margin

    if time.time() < expires_at:
        return cached["access_token"]

    client = load_oauth_client(secrets_path)
    refreshed = _refresh(client, cached["refresh_token"])
    refreshed.setdefault("refresh_token", cached["refresh_token"])  # not always re-sent
    _save_tokens(token_cache_path, refreshed)
    return refreshed["access_token"]
