"""OAuth 2.0 access tokens for the ``oauth2_client_credentials`` and ``oauth2_refresh_token`` auth methods.

The token endpoint, scope and audience come from the connection's auth fields, falling back to the definition's
``oauth2`` block for that method. Tokens are reused until a minute before they expire. Definitions reference the
result as ``{{token.accessToken}}`` and ``{{token.tokenType}}``.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from . import json_util as J
from . import template as T
from .errors import AUTH_FAILED, AgentFrontierError, root_message


class TokenService:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[dict, float]] = {}
        self._lock = threading.Lock()

    def token(self, provider: str, auth: Mapping[str, str], defaults: Any, vars: T.Vars) -> dict:
        if provider not in ("oauth2_client_credentials", "oauth2_refresh_token"):
            return {}
        token_url = _pick(auth.get("tokenUrl"), defaults, "tokenUrl", vars)
        scope = _pick(auth.get("scope"), defaults, "scope", vars)
        audience = _pick(auth.get("audience"), defaults, "audience", vars)
        client_id = _pick(auth.get("clientId"), defaults, "clientId", vars)
        client_secret = _pick(auth.get("clientSecret"), defaults, "clientSecret", vars)
        if token_url is None:
            raise AgentFrontierError.invalid_config("OAuth token URL is not set (auth field tokenUrl)")
        key = "|".join(J.jstr(x) for x in (token_url, client_id, scope, audience, provider))
        with self._lock:
            c = self._cache.get(key)
            if c is not None and time.time() < c[1]:
                return c[0]
        form: dict[str, str | None] = {}
        if provider == "oauth2_client_credentials":
            form["grant_type"] = "client_credentials"
        else:
            form["grant_type"] = "refresh_token"
            form["refresh_token"] = auth.get("refreshToken")
        form["client_id"] = client_id
        form["client_secret"] = client_secret
        form["scope"] = scope
        form["audience"] = audience
        body = "&".join(J.url_encode(k) + "=" + J.url_encode(v) for k, v in form.items() if v)
        req = urllib.request.Request(token_url, data=body.encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "Accept": "application/json"})
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=30) as resp:
                status = resp.status
                text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise AgentFrontierError(AUTH_FAILED, "token endpoint " + str(urllib.parse.urlsplit(token_url).hostname)
                                     + " returned HTTP " + str(e.code)) from e
        except Exception as e:  # noqa: BLE001
            raise AgentFrontierError(AUTH_FAILED, "cannot fetch an OAuth token: " + root_message(e), e) from e
        if status // 100 != 2:
            raise AgentFrontierError(AUTH_FAILED, "token endpoint " + str(urllib.parse.urlsplit(token_url).hostname)
                                     + " returned HTTP " + str(status))
        try:
            doc = J.loads_first(text)
        except ValueError as e:
            raise AgentFrontierError(AUTH_FAILED, "cannot fetch an OAuth token: " + str(e), e) from e
        access = J.text(doc, "access_token")
        if access is None:
            raise AgentFrontierError(AUTH_FAILED, "token endpoint returned no access_token")
        token = {"accessToken": access, "tokenType": J.text(doc, "token_type", "Bearer")}
        ttl = J.integer(doc, "expires_in", 3600)
        with self._lock:
            self._cache[key] = (token, time.time() + max(30, ttl - 60))
        return token


def _pick(own: str | None, defaults: Any, key: str, vars: T.Vars) -> str | None:
    if own is not None and not J.is_blank(own):
        return own
    t = J.text(defaults, key)
    return None if t is None else T.render_text(t, vars, T.STRICT, None)
