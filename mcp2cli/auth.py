#!/usr/bin/env python3
"""
mcp2cli.auth — OAuth 2.1 bearer tokens for MCP endpoints.

An endpoint that requires OAuth answers an unauthenticated request with 401 and
a ``WWW-Authenticate: Bearer resource_metadata="<url>"`` challenge (RFC 9728).
This module turns that challenge into a usable bearer token:

    discover_resource()            protected-resource metadata (RFC 9728)
    discover_authorization_server() authorization-server metadata (RFC 8414)
    login()                        RFC 8252 loopback login: print URL, listen,
                                   exchange the code, store the tokens
    bearer_for()                   stored access token, refreshed when expired

The grant lives on disk keyed by endpoint, mode 0600. Nothing here registers a
client: a deployment names its client via configuration and the module uses it.

Two properties of the realm this was written against shape the code, because
they are common for internal IdPs:

* The client is PUBLIC — no secret exists. Token requests therefore carry only
  ``client_id`` (``token_endpoint_auth_method: none``).
* Dynamic client registration is disabled by policy. ``client_id`` is always
  configuration, never discovered.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

import httpx


ENV_PREFIX = "MCP2CLI_OAUTH_"
DEFAULT_SCOPES: Tuple[str, ...] = ("openid", "profile", "email")
DEFAULT_CALLBACK_PATH = "/callback"
DEFAULT_LOGIN_TIMEOUT_SECONDS = 300
# Refresh this many seconds before the access token actually expires, so a call
# never starts with a token that dies mid-flight.
EXPIRY_SKEW_SECONDS = 30
_HTTP_TIMEOUT_SECONDS = 30.0


class OAuthError(RuntimeError):
    """An OAuth lane could not produce a usable bearer token."""


class AuthChallenge(RuntimeError):
    """The endpoint demanded a bearer token and none is stored.

    Carries the operator-facing hint as its message; the CLI catches it and
    exits with status 2 so scripts can tell 'needs a login' from other errors.
    """


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthConfig:
    """Everything the login flow needs that is not discoverable.

    ``client_id`` comes from the IdP; the rest has working defaults. Note there
    is deliberately no ``client_secret`` field: this module implements the
    public-client flow (PKCE), which is what an operator-run CLI can hold.
    """

    client_id: str
    scopes: Tuple[str, ...] = DEFAULT_SCOPES
    redirect_host: str = "localhost"
    redirect_port: int = 0
    callback_path: str = DEFAULT_CALLBACK_PATH
    login_timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT_SECONDS

    def redirect_uri(self, port: int) -> str:
        """The loopback redirect for a bound port (RFC 8252 §7.3)."""
        return f"http://{self.redirect_host}:{port}{self.callback_path}"


def _env(name: str) -> Optional[str]:
    value = os.environ.get(ENV_PREFIX + name)
    return value if value else None


def _parse_scopes(value: Any) -> Tuple[str, ...]:
    """Accept a list/tuple, or a string split on spaces/commas/pluses."""
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(s for s in re.split(r"[\s,+]+", value) if s)
    if isinstance(value, Iterable):
        return tuple(str(s) for s in value if str(s))
    raise OAuthError(f"cannot read scopes from {value!r}")


def load_config(overrides: Optional[Mapping[str, Any]] = None) -> AuthConfig:
    """Build an :class:`AuthConfig` from environment, then explicit overrides.

    Environment (all optional except the client id):
      MCP2CLI_OAUTH_CLIENT_ID, MCP2CLI_OAUTH_SCOPES,
      MCP2CLI_OAUTH_REDIRECT_HOST, MCP2CLI_OAUTH_REDIRECT_PORT,
      MCP2CLI_OAUTH_CALLBACK_PATH, MCP2CLI_OAUTH_LOGIN_TIMEOUT
    """
    raw: Dict[str, Any] = {}
    for env_name, key in (
        ("CLIENT_ID", "client_id"),
        ("SCOPES", "scopes"),
        ("REDIRECT_HOST", "redirect_host"),
        ("REDIRECT_PORT", "redirect_port"),
        ("CALLBACK_PATH", "callback_path"),
        ("LOGIN_TIMEOUT", "login_timeout_seconds"),
    ):
        value = _env(env_name)
        if value is not None:
            raw[key] = value

    for key, value in (overrides or {}).items():
        if value is None or value == "":
            continue
        raw[key] = value

    client_id = raw.pop("client_id", None)
    if not client_id:
        raise OAuthError(
            f"no OAuth client id: set {ENV_PREFIX}CLIENT_ID or pass --client-id"
        )

    if "scopes" in raw:
        raw["scopes"] = _parse_scopes(raw["scopes"])
    for key in ("redirect_port", "login_timeout_seconds"):
        if key in raw:
            raw[key] = int(raw[key])

    return AuthConfig(client_id=str(client_id), **raw)


# ---------------------------------------------------------------------------
# Discovery (RFC 9728 + RFC 8414)
# ---------------------------------------------------------------------------


def _www_authenticate_field(header: str, field_name: str) -> Optional[str]:
    """Read ``name="value"`` (or unquoted) out of a WWW-Authenticate header."""
    if not header:
        return None
    match = re.search(rf'{field_name}=(?:"([^"]+)"|([^\s,]+))', header)
    if not match:
        return None
    return match.group(1) or match.group(2)


@dataclass(frozen=True)
class ProtectedResource:
    """RFC 9728 protected-resource metadata."""

    resource: str
    authorization_servers: Tuple[str, ...]
    scopes_supported: Tuple[str, ...] = ()
    bearer_methods_supported: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthorizationServer:
    """The subset of authorization-server metadata the flow uses."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: Optional[str] = None
    device_authorization_endpoint: Optional[str] = None
    scopes_supported: Tuple[str, ...] = ()
    grant_types_supported: Tuple[str, ...] = ()
    token_endpoint_auth_methods_supported: Tuple[str, ...] = ()


def probe_resource_metadata_url(endpoint: str) -> Optional[str]:
    """Return the ``resource_metadata`` URL of ``endpoint``'s 401 challenge.

    Returns None when the endpoint does not challenge at all — i.e. it needs no
    OAuth and the caller should send no Authorization header.
    """
    request = {
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "mcp2cli", "version": "0.1"},
        },
        "jsonrpc": "2.0",
        "id": 1,
    }
    try:
        response = httpx.post(
            endpoint,
            json=request,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            timeout=_HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise OAuthError(f"could not reach {endpoint}: {exc}") from exc

    if response.status_code not in (401, 403):
        return None
    return _www_authenticate_field(
        response.headers.get("WWW-Authenticate", ""), "resource_metadata"
    )


def _candidate_resource_metadata_urls(
    www_auth_url: Optional[str], endpoint: str
) -> List[str]:
    """Discovery order for protected-resource metadata (RFC 9728 §3.1)."""
    urls: List[str] = []
    if www_auth_url:
        urls.append(www_auth_url)
    parsed = urllib.parse.urlparse(endpoint)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.path and parsed.path != "/":
        urls.append(f"{origin}/.well-known/oauth-protected-resource{parsed.path}")
    urls.append(f"{origin}/.well-known/oauth-protected-resource")
    return urls


def discover_resource(
    http: httpx.Client,
    endpoint: str,
    www_auth_url: Optional[str] = None,
) -> ProtectedResource:
    """Resolve the protected-resource metadata for an MCP endpoint.

    ``www_auth_url`` short-circuits re-probing when the caller already holds the
    challenge (the client does, on a 401 it just received).
    """
    if www_auth_url is None:
        www_auth_url = probe_resource_metadata_url(endpoint)

    tried: List[str] = []
    for url in _candidate_resource_metadata_urls(www_auth_url, endpoint):
        tried.append(url)
        try:
            response = http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            continue
        if response.status_code == 404:
            continue
        if response.status_code != 200:
            continue
        try:
            body = response.json()
        except ValueError:
            continue
        servers = tuple(body.get("authorization_servers") or ())
        if not servers:
            continue
        return ProtectedResource(
            resource=str(body.get("resource") or endpoint),
            authorization_servers=servers,
            scopes_supported=_parse_scopes(body.get("scopes_supported")),
            bearer_methods_supported=_parse_scopes(body.get("bearer_methods_supported")),
        )

    if not tried:
        raise OAuthError(
            f"{endpoint} answered 401/403 without a resource_metadata challenge"
        )
    raise OAuthError(
        "no protected-resource metadata found; tried: " + ", ".join(tried)
    )


def _candidate_issuer_metadata_urls(issuer: str) -> List[str]:
    """Discovery order for authorization-server metadata (RFC 8414 §3.1)."""
    issuer = issuer.rstrip("/")
    parsed = urllib.parse.urlparse(issuer)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    candidates = [
        f"{issuer}/.well-known/oauth-authorization-server",
        f"{issuer}/.well-known/openid-configuration",
    ]
    if path:
        candidates.append(f"{origin}/.well-known/oauth-authorization-server{path}")
        candidates.append(f"{origin}/.well-known/openid-configuration{path}")
    return candidates


def discover_authorization_server(
    http: httpx.Client, issuer: str
) -> AuthorizationServer:
    """Resolve the authorization-server metadata of an issuer."""
    tried: List[str] = []
    for url in _candidate_issuer_metadata_urls(issuer):
        tried.append(url)
        try:
            response = http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            continue
        if response.status_code != 200:
            continue
        try:
            body = response.json()
        except ValueError:
            continue
        authorization_endpoint = body.get("authorization_endpoint")
        token_endpoint = body.get("token_endpoint")
        if not authorization_endpoint or not token_endpoint:
            continue
        return AuthorizationServer(
            issuer=str(body.get("issuer") or issuer),
            authorization_endpoint=str(authorization_endpoint),
            token_endpoint=str(token_endpoint),
            registration_endpoint=body.get("registration_endpoint"),
            device_authorization_endpoint=body.get("device_authorization_endpoint"),
            scopes_supported=_parse_scopes(body.get("scopes_supported")),
            grant_types_supported=_parse_scopes(body.get("grant_types_supported")),
            token_endpoint_auth_methods_supported=_parse_scopes(
                body.get("token_endpoint_auth_methods_supported")
            ),
        )
    raise OAuthError(
        f"no authorization-server metadata for issuer {issuer}; tried: "
        + ", ".join(tried)
    )


# ---------------------------------------------------------------------------
# PKCE + loopback listener
# ---------------------------------------------------------------------------


def generate_pkce() -> Tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


_SUCCESS_BODY = (
    b"<!doctype html><meta charset=utf-8><title>mcp2cli</title>"
    b"<body style=\"font:16px system-ui;margin:3rem\">"
    b"<h3>Signed in.</h3><p>You can close this tab and return to the terminal.</p>"
)


class CallbackListener:
    """Loopback HTTP listener that catches the authorization redirect.

    Binds before the authorization URL is built, because RFC 8252 requires the
    redirect URI to carry the port that was actually bound (an ephemeral port
    is fine and is the default — ``redirect_port=0``).
    """

    def __init__(self, host: str, port: int, path: str):
        self._path = path
        self._params: Optional[Dict[str, str]] = None
        self._event = threading.Event()
        try:
            # Threading, not single-threaded: one held connection must never be
            # able to block the only handler thread while the flow waits.
            self._server = ThreadingHTTPServer((host, port), self._build_handler())
        except OSError as exc:
            raise OAuthError(f"cannot listen on {host}:{port}: {exc}") from exc
        self._server.daemon_threads = True
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _build_handler(self) -> type:
        listener = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # Bound every socket wait. An idle keep-alive connection then
            # releases its thread instead of holding it until the process
            # exits, and shutdown() can never wait forever on such a handler.
            timeout = 5

            def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != listener._path:
                    self._respond(404, b"not found")
                    return
                if listener._params is None:
                    # First callback wins (RFC 8252 7.6). Browsers retry, and a
                    # hand-pasted callback usually carries a truncated query;
                    # a later request must never overwrite the real code.
                    listener._params = dict(urllib.parse.parse_qsl(parsed.query))
                    listener._event.set()
                self._respond(200, _SUCCESS_BODY)

            def _respond(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # Answer once and go away. Without this the HTTP/1.1 handler
                # loops back into readline and the connection never closes,
                # which strands the client (browser or code exchange) even
                # though the response itself was complete.
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def log_message(self, *args: Any) -> None:
                """Silence the default stderr access log."""

        return _Handler

    def wait(self, timeout_seconds: float) -> Dict[str, str]:
        """Block until the browser hits the callback path."""
        if not self._event.wait(timeout_seconds):
            raise OAuthError(
                f"no callback within {timeout_seconds:.0f}s — the login link was "
                "not completed"
            )
        return dict(self._params or {})

    def close(self) -> None:
        # shutdown() stops the accept loop and waits for it to notice; the
        # bound above keeps that wait finite even if a client is mid-request.
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


@dataclass
class StoredTokens:
    """One endpoint's grant, as persisted and as used.

    ``expires_at`` / ``refresh_expires_at`` are absolute epoch seconds; they are
    what ``status`` reports and what decides whether a refresh is needed.
    """

    access_token: str
    token_type: str = "Bearer"
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None
    refresh_expires_at: Optional[float] = None
    scopes: Tuple[str, ...] = ()
    resource: str = ""
    issuer: str = ""
    token_endpoint: str = ""
    client_id: str = ""
    obtained_at: float = field(default_factory=time.time)

    def is_valid(self, now: Optional[float] = None) -> bool:
        if not self.access_token:
            return False
        if self.expires_at is None:
            # The IdP sent no expires_in: treat the token as usable until the
            # server rejects it.
            return True
        return (now if now is not None else time.time()) < (
            self.expires_at - EXPIRY_SKEW_SECONDS
        )

    def can_refresh(self, now: Optional[float] = None) -> bool:
        if not self.refresh_token:
            return False
        if self.refresh_expires_at is None:
            return True
        # No skew here: a refresh token is used exactly once, at the moment it
        # is needed, so testing it against the IdP is better than guessing.
        return (now if now is not None else time.time()) < self.refresh_expires_at

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "StoredTokens":
        body = json.loads(raw)
        if "scopes" in body:
            body["scopes"] = tuple(body["scopes"])
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in body.items() if k in known})


def default_cache_dir() -> Path:
    """Where grants live: ``$MCP2CLI_OAUTH_CACHE_DIR`` or ``~/.cache/mcp2cli/oauth``."""
    override = _env("CACHE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "mcp2cli" / "oauth"


def normalize_endpoint(endpoint: str) -> str:
    """Cache identity of an endpoint: trailing slashes carry no meaning."""
    return endpoint.rstrip("/")


class TokenStore:
    """Grants on disk, one file per endpoint, mode 0600."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.dir = Path(cache_dir) if cache_dir else default_cache_dir()

    def path_for(self, endpoint: str) -> Path:
        digest = hashlib.sha256(normalize_endpoint(endpoint).encode("utf-8")).hexdigest()[:16]
        return self.dir / f"{digest}.json"

    def load(self, endpoint: str) -> Optional[StoredTokens]:
        path = self.path_for(endpoint)
        if not path.exists():
            return None
        try:
            return StoredTokens.from_json(path.read_text(encoding="utf-8"))
        except (ValueError, TypeError):
            return None

    def save(self, endpoint: str, tokens: StoredTokens) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        path = self.path_for(endpoint)
        # Write-then-rename so a concurrent reader never sees a partial file.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(tokens.to_json(), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        return path

    def delete(self, endpoint: str) -> bool:
        path = self.path_for(endpoint)
        if path.exists():
            path.unlink()
            return True
        return False

    @contextlib.contextmanager
    def lock(self, endpoint: str):
        """Serialize refreshes for one endpoint across processes."""
        self.dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.path_for(endpoint).with_suffix(".lock")
        handle = open(lock_path, "w")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


# ---------------------------------------------------------------------------
# Token requests
# ---------------------------------------------------------------------------


def _tokens_from_response(
    response: httpx.Response,
    *,
    resource: str,
    issuer: str,
    token_endpoint: str,
    client_id: str,
    fallback: Optional[StoredTokens] = None,
) -> StoredTokens:
    """Turn a token-endpoint response into :class:`StoredTokens`.

    A refresh response may omit ``refresh_token`` (Keycloak does), in which case
    the previously held one stays in force — hence ``fallback``.
    """
    if response.status_code != 200:
        raise OAuthError(
            f"token request failed ({response.status_code}): {response.text.strip()}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise OAuthError("token endpoint returned a non-JSON body") from exc

    access_token = body.get("access_token")
    if not access_token:
        raise OAuthError(f"token response has no access_token: {body}")

    now = time.time()
    expires_in = body.get("expires_in")
    refresh_expires_in = body.get("refresh_expires_in")
    return StoredTokens(
        access_token=str(access_token),
        token_type=str(body.get("token_type") or "Bearer"),
        refresh_token=(
            str(body["refresh_token"]) if body.get("refresh_token")
            else (fallback.refresh_token if fallback else None)
        ),
        expires_at=(float(expires_in) + now) if expires_in is not None else None,
        refresh_expires_at=(
            float(refresh_expires_in) + now if refresh_expires_in is not None else None
        ),
        scopes=_parse_scopes(body.get("scope"))
        or (fallback.scopes if fallback else ()),
        resource=resource,
        issuer=issuer,
        token_endpoint=token_endpoint,
        client_id=client_id,
        obtained_at=now,
    )


def exchange_code(
    http: httpx.Client,
    *,
    server: AuthorizationServer,
    config: AuthConfig,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    resource: str,
) -> StoredTokens:
    """Redeem an authorization code for tokens (public client: client_id only)."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": config.client_id,
        "code_verifier": code_verifier,
        "resource": resource,
    }
    response = http.post(
        server.token_endpoint,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    return _tokens_from_response(
        response,
        resource=resource,
        issuer=server.issuer,
        token_endpoint=server.token_endpoint,
        client_id=config.client_id,
    )


def refresh_tokens(http: httpx.Client, tokens: StoredTokens) -> StoredTokens:
    """Exchange a refresh token for a new access token."""
    if not tokens.refresh_token:
        raise OAuthError("stored grant has no refresh token")
    if not tokens.token_endpoint:
        raise OAuthError("stored grant has no token endpoint")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
        "client_id": tokens.client_id,
        "resource": tokens.resource,
    }
    response = http.post(
        tokens.token_endpoint,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    return _tokens_from_response(
        response,
        resource=tokens.resource,
        issuer=tokens.issuer,
        token_endpoint=tokens.token_endpoint,
        client_id=tokens.client_id,
        fallback=tokens,
    )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoginPrompt:
    """What the operator needs in order to complete the login."""

    authorization_url: str
    redirect_uri: str
    resource: str
    issuer: str
    scopes: Tuple[str, ...]
    timeout_seconds: int


def build_authorization_url(
    server: AuthorizationServer,
    config: AuthConfig,
    *,
    redirect_uri: str,
    resource: str,
    state: str,
    code_challenge: str,
    scopes: Tuple[str, ...],
) -> str:
    """Build the authorization request (PKCE S256 + RFC 8707 ``resource``)."""
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "resource": resource,
    }
    return f"{server.authorization_endpoint}?{urllib.parse.urlencode(params)}"


def _default_announce(prompt: LoginPrompt) -> None:
    """Print the login link to stdout; keep progress on stderr."""
    print(prompt.authorization_url, flush=True)
    print(
        f"\nmcp2cli: waiting for the callback on {prompt.redirect_uri} "
        f"(up to {prompt.timeout_seconds}s)",
        file=sys.stderr,
        flush=True,
    )


def login(
    endpoint: str,
    *,
    config: Optional[AuthConfig] = None,
    open_browser: bool = False,
    announce: Callable[[LoginPrompt], None] = _default_announce,
    store: Optional[TokenStore] = None,
) -> StoredTokens:
    """Run the RFC 8252 loopback flow and persist the resulting grant.

    Prints the authorization URL, waits on a loopback listener for the browser
    to deliver the code, exchanges it, and writes the grant into ``store``.
    """
    cfg = config or load_config()
    tokens_dir = store or TokenStore()

    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=True) as http:
        resource = discover_resource(http, endpoint)
        if not resource.authorization_servers:
            raise OAuthError(
                f"{resource.resource} advertises no authorization server"
            )
        server = discover_authorization_server(
            http, resource.authorization_servers[0]
        )

        scopes = cfg.scopes or resource.scopes_supported or DEFAULT_SCOPES
        listener = CallbackListener(
            cfg.redirect_host, cfg.redirect_port, cfg.callback_path
        )
        redirect_uri = cfg.redirect_uri(listener.port)
        verifier, challenge = generate_pkce()
        state = secrets.token_urlsafe(24)
        prompt = LoginPrompt(
            authorization_url=build_authorization_url(
                server,
                cfg,
                redirect_uri=redirect_uri,
                resource=resource.resource,
                state=state,
                code_challenge=challenge,
                scopes=scopes,
            ),
            redirect_uri=redirect_uri,
            resource=resource.resource,
            issuer=server.issuer,
            scopes=scopes,
            timeout_seconds=cfg.login_timeout_seconds,
        )
        announce(prompt)
        if open_browser:
            webbrowser.open(prompt.authorization_url)
        try:
            params = listener.wait(cfg.login_timeout_seconds)
        finally:
            listener.close()

        error = params.get("error")
        if error:
            detail = params.get("error_description") or ""
            raise OAuthError(f"authorization failed: {error} {detail}".strip())
        returned_state = params.get("state")
        if not returned_state or not secrets.compare_digest(returned_state, state):
            raise OAuthError("state mismatch on callback — refusing the code")
        code = params.get("code")
        if not code:
            raise OAuthError("callback carried no authorization code")

        tokens = exchange_code(
            http,
            server=server,
            config=cfg,
            code=code,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            resource=resource.resource,
        )

    tokens_dir.save(endpoint, tokens)
    return tokens


# ---------------------------------------------------------------------------
# Using a stored grant
# ---------------------------------------------------------------------------


def bearer_for(
    endpoint: str,
    *,
    store: Optional[TokenStore] = None,
    warn: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """Return an access token for ``endpoint``, refreshing it when expired.

    Returns None when this endpoint has no stored grant — the caller then sends
    no Authorization header, which is what non-OAuth endpoints expect. Never
    starts a login: minting a grant is an explicit, operator-visible action.

    A refresh that cannot be completed is reported through ``warn`` and yields
    None, so the resulting 401 carries the actionable hint instead of a stack
    trace from deep inside a token request.
    """
    tokens_dir = store or TokenStore()
    tokens = tokens_dir.load(endpoint)
    if tokens is None:
        return None
    if tokens.is_valid():
        return tokens.access_token

    report = warn or (lambda message: print(message, file=sys.stderr, flush=True))
    if not tokens.can_refresh():
        report(
            f"mcp2cli: stored grant for {endpoint} cannot be refreshed "
            "(no refresh token, or it expired)"
        )
        return None

    try:
        with tokens_dir.lock(endpoint):
            # Another process may have refreshed while we waited for the lock.
            tokens = tokens_dir.load(endpoint) or tokens
            if tokens.is_valid():
                return tokens.access_token
            with httpx.Client(
                timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=True
            ) as http:
                refreshed = refresh_tokens(http, tokens)
            tokens_dir.save(endpoint, refreshed)
            return refreshed.access_token
    except (OAuthError, httpx.HTTPError) as exc:
        report(f"mcp2cli: could not refresh the grant for {endpoint}: {exc}")
        return None


def challenge_hint(www_authenticate: str, endpoint: str) -> Optional[str]:
    """An actionable message for a 401/403 bearer challenge, else None.

    This is what turns "HTTP 401" into "run this command" for an operator who
    has never seen the OAuth lane before.
    """
    metadata_url = _www_authenticate_field(www_authenticate or "", "resource_metadata")
    if metadata_url is None and "Bearer" not in (www_authenticate or ""):
        return None
    return (
        f"{endpoint} requires an OAuth bearer token.\n"
        f"  Mint one with:  mcp2cli auth login --endpoint {endpoint}\n"
        f"  Inspect it with: mcp2cli auth status --endpoint {endpoint}"
    )


def status(
    endpoint: str, *, store: Optional[TokenStore] = None
) -> Dict[str, Any]:
    """A JSON-friendly view of the stored grant (or the absence of one)."""
    tokens_dir = store or TokenStore()
    tokens = tokens_dir.load(endpoint)
    if tokens is None:
        return {"endpoint": endpoint, "configured": False, "path": str(tokens_dir.path_for(endpoint))}
    now = time.time()
    return {
        "endpoint": endpoint,
        "configured": True,
        "path": str(tokens_dir.path_for(endpoint)),
        "resource": tokens.resource,
        "issuer": tokens.issuer,
        "client_id": tokens.client_id,
        "scopes": list(tokens.scopes),
        "valid": tokens.is_valid(now),
        "expires_in": None if tokens.expires_at is None else round(tokens.expires_at - now),
        "refreshable": tokens.can_refresh(now),
        "refresh_expires_in": (
            None
            if tokens.refresh_expires_at is None
            else round(tokens.refresh_expires_at - now)
        ),
    }
