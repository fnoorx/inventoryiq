"""StockX OAuth token cache with refresh persisted to the local .env file."""

from datetime import UTC, datetime, timedelta
import os
import threading

from dotenv import load_dotenv, set_key
import requests

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
ENV_PATH = os.path.join(ROOT_DIR, ".env")
ACCESS_TOKEN_URL = "https://accounts.stockx.com/oauth/token"

token_store = {
    "access_token": os.getenv("STOCKX_ACCESS_TOKEN"),
    "refresh_token": os.getenv("STOCKX_REFRESH_TOKEN"),
    "expires_at": None,
}
_token_refresh_lock = threading.RLock()
TOKEN_REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_TOKEN_EXPIRES_IN_SECONDS = 12 * 60 * 60


def reload_env():
    load_dotenv(ENV_PATH, override=True)


def save_tokens_to_env(access_token, refresh_token):
    """Persist tokens to .env so they survive restarts."""

    set_key(ENV_PATH, "STOCKX_ACCESS_TOKEN", access_token)
    set_key(ENV_PATH, "STOCKX_REFRESH_TOKEN", refresh_token)


def store_tokens(data):
    """Update in-memory store and .env from a token response."""

    token_store["access_token"] = data["access_token"]
    token_store["refresh_token"] = data.get("refresh_token", token_store["refresh_token"])
    expires_in = data.get("expires_in", DEFAULT_TOKEN_EXPIRES_IN_SECONDS)
    token_store["expires_at"] = datetime.now(UTC) + timedelta(seconds=expires_in - 300)

    save_tokens_to_env(
        token_store["access_token"],
        token_store["refresh_token"],
    )
    reload_env()
    print(f"[token] stored - expires at {token_store['expires_at'].isoformat()}")


def refresh_access_token():
    with _token_refresh_lock:
        if not token_store["refresh_token"]:
            print("[token] no refresh token available - login required")
            return False

        resp = requests.post(
            ACCESS_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_id": os.getenv("STOCKX_CLIENT_ID"),
                "client_secret": os.getenv("STOCKX_CLIENT_SECRET"),
                "refresh_token": token_store["refresh_token"],
            },
            timeout=TOKEN_REQUEST_TIMEOUT_SECONDS,
        )

        if resp.status_code == 200:
            store_tokens(resp.json())
            print("[token] refreshed successfully")
            return True

        print(f"[token] refresh failed ({resp.status_code})")
        token_store["access_token"] = None
        token_store["expires_at"] = None
        return False


def get_access_token():
    return token_store["access_token"]


def token_needs_refresh(now: datetime | None = None) -> bool:
    if not token_store["access_token"]:
        return True
    expires_at = token_store["expires_at"]
    return bool(expires_at and (now or datetime.now(UTC)) >= expires_at)


def get_valid_access_token(rejected_token: str | None = None):
    """Return a usable token, refreshing once per expiry or rejected token."""

    current_token = token_store["access_token"]
    if rejected_token is None and current_token and not token_needs_refresh():
        return current_token

    with _token_refresh_lock:
        current_token = token_store["access_token"]
        if rejected_token is not None:
            # Another request may already have replaced the rejected token.
            if current_token and current_token != rejected_token:
                return current_token
        elif current_token and not token_needs_refresh():
            return current_token

        if refresh_access_token():
            return token_store["access_token"]
        return None


def ensure_valid_token():
    """Ensure the process has a usable StockX access token.

    Supply your own StockX tokens through environment configuration.
    """
    reload_env()
    token_store["access_token"] = os.getenv("STOCKX_ACCESS_TOKEN")
    token_store["refresh_token"] = os.getenv("STOCKX_REFRESH_TOKEN")

    if token_store["refresh_token"]:
        print("[startup] found existing refresh token - attempting refresh")
        if refresh_access_token():
            return token_store["access_token"]

    return token_store["access_token"]
