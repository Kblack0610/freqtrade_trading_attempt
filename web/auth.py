"""Sign in with Forgejo: the OAuth2 authorization-code flow, and a signed session cookie.

The cookie holds the Forgejo login and an expiry, signed with SESSION_KEY (HMAC-SHA256), so
the site keeps no session store and a restart signs nobody out. Rotating SESSION_KEY signs
everyone out. Only logins in ALLOWED_USERS get a session.
"""

from collections.abc import Mapping

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse

import httpx

COOKIE = "trader_session"
STATE_COOKIE = "trader_oauth_state"
SESSION_DAYS = 30


class Config:
    def __init__(self, env: Mapping[str, str] | None = None):
        env = os.environ if env is None else env
        self.base_url = env.get("TRADER_WEB_BASE_URL", "http://localhost:8000").rstrip("/")
        # The browser goes to Forgejo's public ROOT_URL host; the token and user calls are
        # server-side and may use the in-cluster service instead.
        self.forgejo_url = env.get("FORGEJO_URL", "").rstrip("/")
        self.forgejo_api_url = (env.get("FORGEJO_API_URL") or self.forgejo_url).rstrip("/")
        self.client_id = env.get("OAUTH_CLIENT_ID", "")
        self.client_secret = env.get("OAUTH_CLIENT_SECRET", "")
        self.session_key = env.get("SESSION_KEY", "").encode()
        self.allowed = {u.strip() for u in env.get("ALLOWED_USERS", "kblack0610").split(",") if u.strip()}
        self.secure = self.base_url.startswith("https://")

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url}/oauth/callback"

    def check(self) -> None:
        missing = [k for k, v in (("FORGEJO_URL", self.forgejo_url), ("OAUTH_CLIENT_ID", self.client_id),
                                  ("OAUTH_CLIENT_SECRET", self.client_secret), ("SESSION_KEY", self.session_key)) if not v]
        if missing:
            raise SystemExit(f"trader-web: missing {' '.join(missing)}")


def _sign(key: bytes, payload: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode().rstrip("=")


def make_session(cfg: Config, user: str, now: float | None = None) -> str:
    exp = int((time.time() if now is None else now) + SESSION_DAYS * 86400)
    payload = base64.urlsafe_b64encode(json.dumps({"u": user, "exp": exp}).encode()).decode().rstrip("=")
    return f"{payload}.{_sign(cfg.session_key, payload.encode())}"


def read_session(cfg: Config, cookie: str | None, now: float | None = None) -> str | None:
    """The signed-in login, or None for a missing, forged, expired or no-longer-allowed cookie."""
    if not cookie or "." not in cookie or not cfg.session_key:
        return None
    payload, sig = cookie.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(cfg.session_key, payload.encode())):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    if data.get("exp", 0) < (time.time() if now is None else now) or data.get("u") not in cfg.allowed:
        return None
    return data["u"]


def authorize_url(cfg: Config) -> tuple[str, str]:
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode({"client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri,
                                    "response_type": "code", "state": state})
    return f"{cfg.forgejo_url}/login/oauth/authorize?{query}", state


def login_for_code(cfg: Config, code: str, client: httpx.Client | None = None) -> str:
    """Exchange an authorization code for the Forgejo login it belongs to."""
    c = client or httpx.Client(timeout=15)
    tok = c.post(f"{cfg.forgejo_api_url}/login/oauth/access_token",
                 data={"grant_type": "authorization_code", "code": code, "redirect_uri": cfg.redirect_uri,
                       "client_id": cfg.client_id, "client_secret": cfg.client_secret},
                 headers={"Accept": "application/json"})
    tok.raise_for_status()
    user = c.get(f"{cfg.forgejo_api_url}/api/v1/user",
                 headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
    user.raise_for_status()
    return user.json()["login"]
