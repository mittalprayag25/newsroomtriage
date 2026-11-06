import json
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# Scopes the 30-second job needs. calendar.events is the write scope;
# the readonly calendar scopes on the MCP setup page cannot create an event.
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
)


class AuthError(Exception):
    pass


def authorization_url(client_id: str, redirect_uri: str, state: str, scopes: tuple[str, ...] = SCOPES) -> str:
    # Google issues a refresh token only when the authorization request
    # includes access_type=offline. The OIDC offline_access scope does not.
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
    )
    return f"{AUTH_URL}?{query}"


def refresh_form(client_id: str, client_secret: str, refresh_token: str) -> dict[str, str]:
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }


def exchange_form(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict[str, str]:
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }


class GoogleAuth:
    """Keeps an access token warm from the stored refresh token."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: Path,
        http: httpx.Client | None = None,
    ):
        if not client_id or not client_secret:
            raise AuthError(
                "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env or the environment. "
                "Use a standard Google Web application OAuth client."
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = token_path
        self._http = http or httpx.Client(timeout=30)
        self._owns_http = http is None
        self._access_token = ""
        self._deadline = 0.0

    def access_token(self) -> str:
        if self._access_token and time.monotonic() < self._deadline:
            return self._access_token
        refresh_token = self._read_refresh_token()
        response = self._http.post(
            TOKEN_URL,
            data=refresh_form(self.client_id, self.client_secret, refresh_token),
        )
        payload = _json_body(response)
        if response.status_code >= 400:
            detail = payload.get("error_description") or payload.get("error") or response.status_code
            raise AuthError(f"Google refused to refresh the sign-in: {detail}")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise AuthError("Google returned no access token.")
        self._access_token = token
        expires_in = payload.get("expires_in") or 3600
        try:
            lifetime = int(expires_in)
        except (TypeError, ValueError):
            lifetime = 3600
        # Refresh five minutes early so a tool call does not race the expiry.
        self._deadline = time.monotonic() + max(lifetime - 300, 60)
        rotated = payload.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != refresh_token:
            self._write_refresh_token(rotated)
        return token

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _read_refresh_token(self) -> str:
        if not self.token_path.is_file():
            raise AuthError(
                f"No refresh token at {self.token_path}. Run python -m app.auth once, "
                "then the 30-second job can stay signed in."
            )
        try:
            payload = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthError(f"The refresh token file could not be read: {self.token_path}") from exc
        token = payload.get("refresh_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise AuthError(f"The refresh token file has no refresh_token: {self.token_path}")
        return token

    def _write_refresh_token(self, refresh_token: str) -> None:
        save_refresh_token(self.token_path, refresh_token, self.client_id)


def save_refresh_token(path: Path, refresh_token: str, client_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "scopes": list(SCOPES),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def login(client_id: str, client_secret: str, redirect_uri: str, token_path: Path) -> None:
    if not client_id or not client_secret:
        raise AuthError(
            "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env or the environment. "
            "In Google Auth Platform, create a Web application client, add this redirect URI, "
            "then run sign-in again."
        )
    _check_redirect(redirect_uri)
    state = secrets.token_urlsafe(24)
    url = authorization_url(client_id, redirect_uri, state)
    code = _wait_for_code(url, redirect_uri, state)
    response = httpx.post(
        TOKEN_URL,
        data=exchange_form(client_id, client_secret, code, redirect_uri),
        timeout=30,
    )
    payload = _json_body(response)
    if response.status_code >= 400:
        detail = payload.get("error_description") or payload.get("error") or response.status_code
        raise AuthError(f"Google refused the authorization code: {detail}")
    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise AuthError(
            "Google did not return a refresh token. Remove the app's access in the Google Account "
            "page and sign in again. The request already asks for access_type=offline and prompt=consent."
        )
    save_refresh_token(token_path, refresh_token, client_id)


def _check_redirect(redirect_uri: str) -> None:
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise AuthError("The redirect URI has to be an http://127.0.0.1 or localhost address.")
    if parsed.path != "/oauth/callback":
        raise AuthError("The redirect URI path has to be /oauth/callback.")
    if not parsed.port:
        raise AuthError("The redirect URI needs a port.")


def _wait_for_code(url: str, redirect_uri: str, state: str) -> str:
    parsed = urlparse(redirect_uri)
    port = parsed.port or 80
    box: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            incoming = urlparse(self.path)
            if incoming.path != "/oauth/callback":
                self.send_response(404)
                self.end_headers()
                return
            query = parse_qs(incoming.query)
            box["code"] = (query.get("code") or [""])[0]
            box["state"] = (query.get("state") or [""])[0]
            box["error"] = (query.get("error") or [""])[0]
            body = b"Signed in. You can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise AuthError(f"Could not listen on {redirect_uri}: {exc}") from exc
    server.timeout = 1
    print(url)
    print("Open that URL, sign in, and allow the newsroom agent.")
    webbrowser.open(url)
    deadline = time.monotonic() + 180
    try:
        while time.monotonic() < deadline and not box.get("code") and not box.get("error"):
            server.handle_request()
    finally:
        server.server_close()
    if box.get("error"):
        raise AuthError(f"Google returned {box['error']}.")
    if not box.get("code"):
        raise AuthError("No authorization code arrived. The sign-in window is three minutes.")
    if box.get("state") != state:
        raise AuthError("The sign-in response did not match this request.")
    return box["code"]


def _json_body(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def main() -> int:
    from app.paths import repo_path
    from app.settings import Settings

    settings = Settings.from_env()
    try:
        login(
            settings.google_client_id,
            settings.google_client_secret,
            settings.oauth_redirect_uri,
            repo_path(settings.google_token_path),
        )
    except AuthError as exc:
        print(exc)
        return 1
    print(f"Refresh token saved to {settings.google_token_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
