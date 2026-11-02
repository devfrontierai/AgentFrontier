"""Values derived at connect time for auth methods whose ``provider`` is not ``static`` (spec docs/auth.md):

* ``oauth2_client_credentials``, ``oauth2_refresh_token`` and ``azure_ad``: an OAuth 2.0 access token from a token
  endpoint;
* ``azure_managed_identity``: a token from the Azure instance metadata (or App Service identity) endpoint;
* ``gcp_jwt``: a Google service account's signed JWT exchanged for an access token;
* ``aws_sigv4``: AWS credentials (the fields, the environment or a profile), exchanged through STS ``AssumeRole``
  when a role is given.

Token endpoints, scopes and audiences come from the connection's auth fields, falling back to the definition's
``oauth2`` block for that method. Values are reused until shortly before they expire. Definitions reference them as
``{{token.accessToken}}``, ``{{token.tokenType}}`` (and for AWS ``{{token.accessKeyId}}`` and the like).
"""

from __future__ import annotations

import configparser
import datetime as _dt
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping

from . import json_util as J
from . import template as T
from .errors import AUTH_FAILED, AgentFrontierError, root_message
from .jwt import load_private_key, service_account_jwt
from .sigv4 import Credentials, sign

AZURE_AUTHORITY = "https://login.microsoftonline.com"
AZURE_IMDS = "http://169.254.169.254/metadata/identity/oauth2/token"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class TokenService:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[dict, float]] = {}
        self._lock = threading.Lock()

    def token(self, provider: str, auth: Mapping[str, str], defaults: Any, vars: T.Vars) -> dict:
        if provider in ("oauth2_client_credentials", "oauth2_refresh_token"):
            return self._oauth2(provider, auth, defaults, vars)
        if provider == "azure_ad":
            scope = _pick(auth.get("scope"), defaults, "scope", vars)
            if scope is None:
                return {}
            host = J.text(auth, "authorityHost") or AZURE_AUTHORITY
            tenant = auth.get("tenantId")
            if not tenant:
                raise AgentFrontierError.invalid_config("auth field tenantId is required")
            token_url = host.rstrip("/") + "/" + tenant + "/oauth2/v2.0/token"
            return self._oauth2("oauth2_client_credentials", dict(auth, tokenUrl=token_url, scope=scope), None, vars)
        if provider == "azure_managed_identity":
            return self._managed_identity(auth, defaults, vars)
        if provider == "gcp_jwt":
            return self._gcp(auth, defaults, vars)
        if provider == "aws_sigv4":
            return self._aws(auth, vars)
        return {}

    # ------------------------------------------------------------------------------------------------ cache

    def _cached(self, key: str) -> dict | None:
        with self._lock:
            c = self._cache.get(key)
            if c is not None and time.time() < c[1]:
                return c[0]
        return None

    def _store(self, key: str, value: dict, expires_at: float) -> dict:
        with self._lock:
            self._cache[key] = (value, expires_at)
        return value

    # ------------------------------------------------------------------------------------------------ OAuth 2.0

    def _oauth2(self, provider: str, auth: Mapping[str, str], defaults: Any, vars: T.Vars) -> dict:
        token_url = _pick(auth.get("tokenUrl"), defaults, "tokenUrl", vars)
        scope = _pick(auth.get("scope"), defaults, "scope", vars)
        audience = _pick(auth.get("audience"), defaults, "audience", vars)
        client_id = _pick(auth.get("clientId"), defaults, "clientId", vars)
        client_secret = _pick(auth.get("clientSecret"), defaults, "clientSecret", vars)
        if token_url is None:
            raise AgentFrontierError.invalid_config("OAuth token URL is not set (auth field tokenUrl)")
        key = "|".join(J.jstr(x) for x in (token_url, client_id, scope, audience, provider))
        hit = self._cached(key)
        if hit is not None:
            return hit
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
        doc = _request_json("POST", token_url, _form(form), {"Content-Type": "application/x-www-form-urlencoded"})
        return self._bearer(key, doc)

    def _bearer(self, key: str, doc: Any) -> dict:
        access = J.text(doc, "access_token")
        if access is None:
            raise AgentFrontierError(AUTH_FAILED, "token endpoint returned no access_token")
        token = {"accessToken": access, "tokenType": J.text(doc, "token_type", "Bearer")}
        expires_on = J.get(doc, "expires_on")
        if expires_on is not None and J.jtrim(J.jstr(expires_on)).isdigit():
            ttl = int(J.jtrim(J.jstr(expires_on))) - int(time.time())
        else:
            ttl = J.integer(doc, "expires_in", 3600)
        return self._store(key, token, time.time() + max(30, ttl - 60))

    # ------------------------------------------------------------------------------------------------ Azure

    def _managed_identity(self, auth: Mapping[str, str], defaults: Any, vars: T.Vars) -> dict:
        resource = _pick(None, defaults, "resource", vars)
        if resource is None:
            scope = _pick(auth.get("scope"), defaults, "scope", vars)
            if scope is None:
                return {}
            resource = scope[:-len("/.default")] if scope.endswith("/.default") else scope
        client_id = auth.get("clientId") or None
        endpoint = J.text(auth, "identityEndpoint")
        headers = {"Metadata": "true"}
        version = "2018-02-01"
        if not endpoint and os.environ.get("IDENTITY_ENDPOINT") and os.environ.get("IDENTITY_HEADER"):
            endpoint = os.environ["IDENTITY_ENDPOINT"]
            headers = {"X-IDENTITY-HEADER": os.environ["IDENTITY_HEADER"]}
            version = "2019-08-01"
        endpoint = endpoint or AZURE_IMDS
        key = "|".join(("msi", endpoint, resource, J.jstr(client_id)))
        hit = self._cached(key)
        if hit is not None:
            return hit
        query = {"api-version": version, "resource": resource, "client_id": client_id}
        url = endpoint + ("&" if "?" in endpoint else "?") + _form(query).decode("ascii")
        return self._bearer(key, _request_json("GET", url, None, headers))

    # ------------------------------------------------------------------------------------------------ Google

    def _gcp(self, auth: Mapping[str, str], defaults: Any, vars: T.Vars) -> dict:
        scope = _pick(auth.get("scope"), defaults, "scope", vars)
        if scope is None:
            return {}
        raw = auth.get("serviceAccountJson")
        if not raw:
            raise AgentFrontierError.invalid_config("auth field serviceAccountJson is required")
        try:
            sa = J.loads_first(raw)
        except ValueError as e:
            raise AgentFrontierError.invalid_config("serviceAccountJson is not valid JSON: " + str(e)) from None
        email = J.text(sa, "client_email")
        pem = J.text(sa, "private_key")
        if not email or not pem:
            raise AgentFrontierError.invalid_config("serviceAccountJson needs client_email and private_key")
        token_uri = J.text(sa, "token_uri") or GOOGLE_TOKEN_URI
        key = "|".join(("gcp", token_uri, email, scope))
        hit = self._cached(key)
        if hit is not None:
            return hit
        assertion = service_account_jwt(load_private_key(pem), J.text(sa, "private_key_id"), email, scope, token_uri,
                                        int(time.time()))
        doc = _request_json("POST", token_uri, _form({"grant_type": JWT_BEARER, "assertion": assertion}),
                            {"Content-Type": "application/x-www-form-urlencoded"})
        return self._bearer(key, doc)

    # ------------------------------------------------------------------------------------------------ AWS

    def _aws(self, auth: Mapping[str, str], vars: T.Vars) -> dict:
        region = auth.get("region") or J.jstr(vars.get("region") or "") or os.environ.get("AWS_REGION") \
            or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        creds = _base_aws_credentials(auth)
        role = auth.get("roleArn")
        if role:
            key = "|".join(("sts", creds.access_key_id, role, J.jstr(auth.get("externalId")), region))
            hit = self._cached(key)
            if hit is not None:
                return hit
            creds, expires_at = _assume_role(creds, role, auth.get("externalId") or None, region,
                                             auth.get("stsEndpoint") or None)
            return self._store(key, _aws_token(creds, region), expires_at - 300)
        return _aws_token(creds, region)


def aws_credentials(token: Mapping[str, Any]) -> Credentials:
    """The credentials of an ``aws_sigv4`` token."""
    return Credentials(J.jstr(token.get("accessKeyId")), J.jstr(token.get("secretAccessKey")),
                       None if token.get("sessionToken") is None else J.jstr(token.get("sessionToken")))


def _aws_token(c: Credentials, region: str) -> dict:
    return {"accessKeyId": c.access_key_id, "secretAccessKey": c.secret_access_key, "sessionToken": c.session_token,
            "region": region}


def _base_aws_credentials(auth: Mapping[str, str]) -> Credentials:
    if auth.get("accessKeyId") and auth.get("secretAccessKey"):
        return Credentials(auth["accessKeyId"], auth["secretAccessKey"], auth.get("sessionToken") or None)
    profile = auth.get("profile")
    if profile:
        path = Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or Path.home() / ".aws" / "credentials")
        cp = configparser.ConfigParser()
        cp.read(path, encoding="utf-8")
        if not cp.has_section(profile):
            raise AgentFrontierError.invalid_config("AWS profile '" + profile + "' is not in " + str(path))
        sec = cp[profile]
        if not sec.get("aws_access_key_id") or not sec.get("aws_secret_access_key"):
            raise AgentFrontierError.invalid_config("AWS profile '" + profile + "' has no access keys")
        return Credentials(sec["aws_access_key_id"], sec["aws_secret_access_key"], sec.get("aws_session_token"))
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return Credentials(os.environ["AWS_ACCESS_KEY_ID"], os.environ["AWS_SECRET_ACCESS_KEY"],
                           os.environ.get("AWS_SESSION_TOKEN") or None)
    raise AgentFrontierError.invalid_config("no AWS credentials: set accessKeyId and secretAccessKey, a profile, or "
                                            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")


def _assume_role(creds: Credentials, role: str, external_id: str | None, region: str,
                 endpoint: str | None) -> tuple[Credentials, float]:
    url = endpoint or "https://sts." + region + ".amazonaws.com/"
    form = {"Action": "AssumeRole", "RoleArn": role, "RoleSessionName": "agentfrontier", "Version": "2011-06-15",
            "ExternalId": external_id}
    body = _form(form)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    headers.update(sign("POST", url, headers, body, creds, region, "sts"))
    text = _request_text("POST", url, body, headers)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise AgentFrontierError(AUTH_FAILED, "STS returned an unreadable answer: " + str(e)) from None
    values: dict[str, str] = {}
    for el in root.iter():
        name = el.tag.rsplit("}", 1)[-1]
        if name in ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration") and el.text:
            values[name] = el.text.strip()
    if "AccessKeyId" not in values or "SecretAccessKey" not in values:
        raise AgentFrontierError(AUTH_FAILED, "STS AssumeRole returned no credentials")
    expires = time.time() + 3600
    if "Expiration" in values:
        try:
            expires = _dt.datetime.fromisoformat(values["Expiration"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return Credentials(values["AccessKeyId"], values["SecretAccessKey"], values.get("SessionToken")), expires


# ---------------------------------------------------------------------------------------------------------- HTTP


def _form(pairs: Mapping[str, str | None]) -> bytes:
    return "&".join(J.url_encode(k) + "=" + J.url_encode(v) for k, v in pairs.items() if v).encode("utf-8")


def _request_text(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> str:
    headers = dict(headers)
    headers.setdefault("Accept", "application/json")
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    host = str(urllib.parse.urlsplit(url).hostname)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=30) as resp:
            status = resp.status
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise AgentFrontierError(AUTH_FAILED, "token endpoint " + host + " returned HTTP " + str(e.code)) from e
    except Exception as e:  # noqa: BLE001
        raise AgentFrontierError(AUTH_FAILED, "cannot fetch credentials from " + host + ": " + root_message(e),
                                 e) from e
    if status // 100 != 2:
        raise AgentFrontierError(AUTH_FAILED, "token endpoint " + host + " returned HTTP " + str(status))
    return text


def _request_json(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Any:
    text = _request_text(method, url, body, headers)
    try:
        return J.loads_first(text)
    except ValueError as e:
        raise AgentFrontierError(AUTH_FAILED, "cannot fetch an access token: " + str(e), e) from e


def _pick(own: str | None, defaults: Any, key: str, vars: T.Vars) -> str | None:
    if own is not None and not J.is_blank(own):
        return own
    t = J.text(defaults, key)
    return None if t is None else T.render_text(t, vars, T.STRICT, None)
