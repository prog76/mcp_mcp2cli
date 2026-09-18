"""Tests for the OAuth bearer lane (mcp2cli.auth).

A fake IdP (plain HTTP, loopback) stands in for Keycloak so the whole flow is
exercised for real: 401 challenge, protected-resource discovery, authorization-
server discovery, the RFC 8252 loopback redirect, the PKCE-verified code
exchange, persistence, and refresh.
"""

import hashlib
import json
import os
import stat
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from mcp2cli import auth


RESOURCE_PATH = "/servers/abc123/mcp"


class FakeIdP:
    """Minimal OAuth-protected MCP resource plus its authorization server.

    Serves four things on one loopback port:
      POST {resource_path}  401 + WWW-Authenticate (unless a bearer is presented)
      GET  {prm_path}       protected-resource metadata
      GET  {as_path}        authorization-server metadata
      POST /token           code exchange and refresh
    """

    def __init__(self):
        self.port = 0
        self.origin = ""
        self.resource = ""
        self.requests = []
        self.seen_verifier = None
        self.seen_challenge = None
        self.access_token = "access-1"
        self.refresh_token = "refresh-1"
        self.expires_in = 300
        self.refresh_expires_in = 1800
        self.require_bearer = True
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.resource = self.origin + RESOURCE_PATH
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    # -- URLs ----------------------------------------------------------------

    @property
    def prm_url(self):
        return self.origin + "/.well-known/oauth-protected-resource" + RESOURCE_PATH

    @property
    def as_url(self):
        return self.origin + "/.well-known/oauth-authorization-server"

    @property
    def token_url(self):
        return self.origin + "/token"

    @property
    def authorize_url(self):
        return self.origin + "/authorize"

    # -- handler -------------------------------------------------------------

    def _handler(idp):
        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status, body, headers=None):
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                idp.requests.append(("GET", self.path, self._header_view, {}))
                if parsed.path == RESOURCE_PATH:
                    self._challenge()
                    return
                if parsed.path == "/.well-known/oauth-protected-resource" + RESOURCE_PATH:
                    self._send(200, {
                        "resource": idp.resource,
                        "authorization_servers": [idp.origin],
                        "scopes_supported": ["openid", "profile", "email"],
                        "bearer_methods_supported": ["header"],
                    })
                    return
                if parsed.path == "/.well-known/oauth-authorization-server":
                    self._send(200, {
                        "issuer": idp.origin,
                        "authorization_endpoint": idp.authorize_url,
                        "token_endpoint": idp.token_url,
                        "registration_endpoint": idp.origin + "/register",
                        "scopes_supported": ["openid", "profile", "email", "groups"],
                        "grant_types_supported": ["authorization_code", "refresh_token"],
                        "token_endpoint_auth_methods_supported": ["none"],
                    })
                    return
                self._send(404, {"error": "not_found"})

            def do_POST(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode()
                form = dict(urllib.parse.parse_qsl(raw))
                idp.requests.append(("POST", self.path, self._header_view, form))
                if parsed.path == RESOURCE_PATH:
                    self._challenge()
                    return
                if parsed.path == "/token":
                    self._token(form)
                    return
                self._send(404, {"error": "not_found"})

            @property
            def _header_view(self):
                """Request headers as {lowercase_name: value}, as recorded."""
                return {k.lower(): v for k, v in self.headers.items()}

            @property
            def _authorized(self):
                """True when the request carries the access token we issued."""
                return self.headers.get("Authorization") == f"Bearer {idp.access_token}"

            def _challenge(self):
                if not idp.require_bearer or self._authorized:
                    self._send(200, {"jsonrpc": "2.0", "id": 1, "result": {}})
                    return
                self._send(
                    401,
                    {"detail": "This server requires OAuth authentication"},
                    {"WWW-Authenticate": 'Bearer resource_metadata="' + idp.prm_url + '"'},
                )

            def _token(self, form):
                if form.get("grant_type") == "authorization_code":
                    if form.get("code") != "GOODCODE":
                        self._send(400, {"error": "invalid_grant", "error_description": "Code not valid"})
                        return
                    idp.seen_verifier = form.get("code_verifier")
                    digest = hashlib.sha256((idp.seen_verifier or "").encode("ascii")).digest()
                    import base64

                    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
                    if computed != idp.seen_challenge:
                        self._send(400, {"error": "invalid_grant", "error_description": "PKCE mismatch"})
                        return
                elif form.get("grant_type") == "refresh_token":
                    if form.get("refresh_token") != idp.refresh_token:
                        self._send(400, {"error": "invalid_grant", "error_description": "Invalid refresh token"})
                        return
                    idp.access_token = "access-2"
                else:
                    self._send(400, {"error": "unsupported_grant_type"})
                    return
                self._send(200, {
                    "access_token": idp.access_token,
                    "token_type": "Bearer",
                    "expires_in": idp.expires_in,
                    "refresh_token": idp.refresh_token,
                    "refresh_expires_in": idp.refresh_expires_in,
                    "scope": "openid profile email",
                })

            def log_message(self, *args):
                pass

        return _Handler

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._server.daemon_threads = True


@pytest.fixture
def idp():
    server = FakeIdP()
    yield server
    server.close()


@pytest.fixture
def store(tmp_path):
    return auth.TokenStore(tmp_path / "oauth")


def _play_browser(prompt, idp, code="GOODCODE"):
    """Act as the browser: follow the authorization URL to the loopback URI."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(prompt.authorization_url).query)
    idp.seen_challenge = query["code_challenge"][0]
    assert query["code_challenge_method"] == ["S256"]
    callback = query["redirect_uri"][0]
    url = callback + "?" + urllib.parse.urlencode({
        "code": code,
        "state": query["state"][0],
    })
    httpx.get(url, timeout=10)


# -- PKCE ---------------------------------------------------------------------


def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = auth.generate_pkce()
    assert len(verifier) >= 43
    import base64

    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected
    assert "=" not in challenge


def test_pkce_verifiers_are_unique():
    assert auth.generate_pkce()[0] != auth.generate_pkce()[0]


# -- discovery ----------------------------------------------------------------


def test_probe_reads_resource_metadata_from_challenge(idp):
    assert auth.probe_resource_metadata_url(idp.resource) == idp.prm_url


def test_probe_returns_none_when_endpoint_needs_no_oauth(idp):
    idp.require_bearer = False
    assert auth.probe_resource_metadata_url(idp.resource) is None


def test_discover_resource(idp):
    with httpx.Client(timeout=10) as http:
        resource = auth.discover_resource(http, idp.resource)
    assert resource.resource == idp.resource
    assert resource.authorization_servers == (idp.origin,)
    assert "openid" in resource.scopes_supported


def test_discover_authorization_server(idp):
    with httpx.Client(timeout=10) as http:
        server = auth.discover_authorization_server(http, idp.origin)
    assert server.token_endpoint == idp.token_url
    assert server.authorization_endpoint == idp.authorize_url
    assert "authorization_code" in server.grant_types_supported


def test_issuer_metadata_urls_handle_path_bearing_issuer():
    urls = auth._candidate_issuer_metadata_urls("https://auth.example.com/realms/auth")
    assert urls[0] == "https://auth.example.com/realms/auth/.well-known/oauth-authorization-server"
    assert "https://auth.example.com/.well-known/oauth-authorization-server/realms/auth" in urls


def test_resource_metadata_urls_prefer_challenge_then_path():
    urls = auth._candidate_resource_metadata_urls(
        "https://host/.well-known/oauth-protected-resource/servers/x/mcp",
        "https://host/servers/x/mcp",
    )
    assert urls[0].startswith("https://host/.well-known/oauth-protected-resource/servers/x/mcp")
    assert urls[-1] == "https://host/.well-known/oauth-protected-resource"


# -- config -------------------------------------------------------------------


def test_load_config_requires_client_id(monkeypatch):
    monkeypatch.delenv("MCP2CLI_OAUTH_CLIENT_ID", raising=False)
    with pytest.raises(auth.OAuthError):
        auth.load_config()


def test_load_config_env_then_override(monkeypatch):
    monkeypatch.setenv("MCP2CLI_OAUTH_CLIENT_ID", "from-env")
    monkeypatch.setenv("MCP2CLI_OAUTH_SCOPES", "openid,groups")
    monkeypatch.setenv("MCP2CLI_OAUTH_REDIRECT_PORT", "8765")
    config = auth.load_config()
    assert config.client_id == "from-env"
    assert config.scopes == ("openid", "groups")
    assert config.redirect_port == 8765

    overridden = auth.load_config({"client_id": "from-flag", "scopes": "a b"})
    assert overridden.client_id == "from-flag"
    assert overridden.scopes == ("a", "b")


def test_redirect_uri_uses_bound_port():
    config = auth.load_config({"client_id": "c", "redirect_host": "127.0.0.1"})
    assert config.redirect_uri(4321) == "http://127.0.0.1:4321/callback"


# -- loopback listener --------------------------------------------------------


def test_callback_listener_captures_query():
    listener = auth.CallbackListener("127.0.0.1", 0, "/callback")
    try:
        assert listener.port > 0
        url = f"http://127.0.0.1:{listener.port}/callback?code=abc&state=xyz"
        response = httpx.get(url, timeout=10)
        assert response.status_code == 200
        params = listener.wait(5)
    finally:
        listener.close()
    assert params == {"code": "abc", "state": "xyz"}


def test_callback_listener_404s_other_paths():
    listener = auth.CallbackListener("127.0.0.1", 0, "/callback")
    try:
        response = httpx.get(f"http://127.0.0.1:{listener.port}/nope", timeout=10)
        assert response.status_code == 404
    finally:
        listener.close()


def test_callback_listener_times_out():
    listener = auth.CallbackListener("127.0.0.1", 0, "/callback")
    try:
        with pytest.raises(auth.OAuthError):
            listener.wait(0.3)
    finally:
        listener.close()


# -- token store --------------------------------------------------------------


def test_store_roundtrip(store):
    tokens = auth.StoredTokens(
        access_token="a", refresh_token="r", scopes=("openid",), resource="res"
    )
    path = store.save("https://host/mcp", tokens)
    loaded = store.load("https://host/mcp")
    assert loaded.access_token == "a"
    assert loaded.refresh_token == "r"
    assert loaded.scopes == ("openid",)
    assert loaded.resource == "res"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_store_ignores_trailing_slash(store):
    store.save("https://host/mcp", auth.StoredTokens(access_token="a"))
    assert store.load("https://host/mcp/") is not None


def test_store_missing_returns_none(store):
    assert store.load("https://host/never") is None


def test_store_delete(store):
    store.save("https://host/mcp", auth.StoredTokens(access_token="a"))
    assert store.delete("https://host/mcp") is True
    assert store.delete("https://host/mcp") is False
    assert store.load("https://host/mcp") is None


def test_store_survives_corrupt_file(store):
    path = store.path_for("https://host/mcp")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert store.load("https://host/mcp") is None


def test_store_ignores_unknown_fields(store):
    path = store.path_for("https://host/mcp")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"access_token": "a", "future_field": 1}), encoding="utf-8"
    )
    assert store.load("https://host/mcp").access_token == "a"


# -- token validity -----------------------------------------------------------


def test_is_valid_respects_skew():
    now = time.time()
    fresh = auth.StoredTokens(access_token="a", expires_at=now + 300)
    assert fresh.is_valid(now) is True
    assert fresh.is_valid(now + 280) is False
    never_expires = auth.StoredTokens(access_token="a", expires_at=None)
    assert never_expires.is_valid(now) is True
    assert auth.StoredTokens(access_token="").is_valid(now) is False


def test_can_refresh():
    now = time.time()
    assert auth.StoredTokens(access_token="a").can_refresh(now) is False
    assert auth.StoredTokens(access_token="a", refresh_token="r").can_refresh(now) is True
    expired = auth.StoredTokens(
        access_token="a", refresh_token="r", refresh_expires_at=now - 1
    )
    assert expired.can_refresh(now) is False


# -- full login ---------------------------------------------------------------


def test_login_end_to_end(idp, store):
    prompts = []
    tokens = auth.login(
        idp.resource,
        config=auth.load_config({"client_id": "kubernetes-mcp-cursor"}),
        announce=lambda prompt: (prompts.append(prompt), _play_browser(prompt, idp)),
        store=store,
    )
    assert len(prompts) == 1
    prompt = prompts[0]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(prompt.authorization_url).query)
    assert query["client_id"] == ["kubernetes-mcp-cursor"]
    assert query["resource"] == [idp.resource]
    assert query["response_type"] == ["code"]
    assert prompt.redirect_uri.startswith("http://localhost:")

    assert tokens.access_token == "access-1"
    assert tokens.refresh_token == "refresh-1"
    assert tokens.token_endpoint == idp.token_url
    assert tokens.issuer == idp.origin
    assert tokens.resource == idp.resource
    assert tokens.refresh_expires_at > time.time()
    assert store.load(idp.resource).access_token == "access-1"


def test_login_scopes_come_from_config_then_resource(idp, store):
    prompts = []
    auth.login(
        idp.resource,
        config=auth.load_config({"client_id": "c", "scopes": "openid groups"}),
        announce=lambda prompt: (prompts.append(prompt), _play_browser(prompt, idp)),
        store=store,
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(prompts[0].authorization_url).query)
    assert query["scope"] == ["openid groups"]
    assert prompts[0].scopes == ("openid", "groups")


def test_login_sends_pkce_and_no_client_secret(idp, store):
    auth.login(
        idp.resource,
        config=auth.load_config({"client_id": "c"}),
        announce=lambda prompt: _play_browser(prompt, idp),
        store=store,
    )
    exchange = [r for r in idp.requests if r[0] == "POST" and r[1] == "/token"][0]
    assert exchange[3]["client_id"] == "c"
    assert "client_secret" not in exchange[3]
    assert exchange[3]["redirect_uri"].startswith("http://localhost:")
    assert idp.seen_verifier


def test_login_rejects_state_mismatch(idp, store):
    def play_badly(prompt):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(prompt.authorization_url).query)
        httpx.get(
            query["redirect_uri"][0] + "?"
            + urllib.parse.urlencode({"code": "GOODCODE", "state": "forged"}),
            timeout=10,
        )

    with pytest.raises(auth.OAuthError, match="state mismatch"):
        auth.login(
            idp.resource,
            config=auth.load_config({"client_id": "c"}),
            announce=play_badly,
            store=store,
        )
    assert store.load(idp.resource) is None


def test_login_surfaces_authorization_error(idp, store):
    def play_error(prompt):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(prompt.authorization_url).query)
        httpx.get(
            query["redirect_uri"][0] + "?"
            + urllib.parse.urlencode({
                "error": "access_denied",
                "error_description": "user said no",
                "state": query["state"][0],
            }),
            timeout=10,
        )

    with pytest.raises(auth.OAuthError, match="access_denied"):
        auth.login(
            idp.resource,
            config=auth.load_config({"client_id": "c"}),
            announce=play_error,
            store=store,
        )


def test_login_rejects_bad_code(idp, store):
    with pytest.raises(auth.OAuthError, match="invalid_grant"):
        auth.login(
            idp.resource,
            config=auth.load_config({"client_id": "c"}),
            announce=lambda prompt: _play_browser(prompt, idp, code="BADCODE"),
            store=store,
        )


# -- using a stored grant -----------------------------------------------------


def test_bearer_for_returns_none_without_grant(store):
    assert auth.bearer_for("https://host/mcp", store=store) is None


def test_bearer_for_returns_valid_token(idp, store):
    auth.login(
        idp.resource,
        config=auth.load_config({"client_id": "c"}),
        announce=lambda prompt: _play_browser(prompt, idp),
        store=store,
    )
    assert auth.bearer_for(idp.resource, store=store) == "access-1"


def test_bearer_for_refreshes_expired_token(idp, store):
    store.save(
        idp.resource,
        auth.StoredTokens(
            access_token="stale",
            refresh_token="refresh-1",
            expires_at=time.time() - 5,
            refresh_expires_at=time.time() + 1800,
            token_endpoint=idp.token_url,
            client_id="c",
            resource=idp.resource,
            issuer=idp.origin,
        ),
    )
    token = auth.bearer_for(idp.resource, store=store)
    assert token == "access-2"
    assert store.load(idp.resource).access_token == "access-2"


def test_bearer_for_warns_when_refresh_impossible(store):
    warnings = []
    store.save(
        "https://host/mcp",
        auth.StoredTokens(access_token="stale", expires_at=time.time() - 5),
    )
    assert auth.bearer_for("https://host/mcp", store=store, warn=warnings.append) is None
    assert warnings and "cannot be refreshed" in warnings[0]


def test_bearer_for_warns_on_refresh_failure(idp, store):
    warnings = []
    store.save(
        idp.resource,
        auth.StoredTokens(
            access_token="stale",
            refresh_token="wrong-refresh",
            expires_at=time.time() - 5,
            token_endpoint=idp.token_url,
            client_id="c",
            resource=idp.resource,
        ),
    )
    assert auth.bearer_for(idp.resource, store=store, warn=warnings.append) is None
    assert warnings and "could not refresh" in warnings[0]


# -- hints and status ---------------------------------------------------------


def test_challenge_hint_only_for_bearer_challenges():
    hint = auth.challenge_hint('Bearer resource_metadata="https://h/prm"', "https://h/mcp")
    assert hint and "mcp2cli auth login" in hint
    assert auth.challenge_hint("Basic realm=x", "https://h/mcp") is None
    assert auth.challenge_hint("", "https://h/mcp") is None


def test_status_reports_absence(store):
    body = auth.status("https://host/mcp", store=store)
    assert body["configured"] is False
    assert body["endpoint"] == "https://host/mcp"


def test_status_reports_grant(idp, store):
    auth.login(
        idp.resource,
        config=auth.load_config({"client_id": "c"}),
        announce=lambda prompt: _play_browser(prompt, idp),
        store=store,
    )
    body = auth.status(idp.resource, store=store)
    assert body["configured"] is True
    assert body["valid"] is True
    assert body["refreshable"] is True
    assert body["client_id"] == "c"
    assert body["scopes"] == ["openid", "profile", "email"]
    assert body["expires_in"] > 0
    assert body["refresh_expires_in"] > 0


# -- integration with the client's request path -------------------------------


def test_client_sends_bearer_and_lists_tools(idp, store, monkeypatch):
    """With a grant stored, the client authenticates for real.

    This is the end-to-end claim: login writes a grant, and the next tool-list
    call carries it and gets past the 401 challenge instead of hinting.
    """
    monkeypatch.setenv("MCP2CLI_OAUTH_CACHE_DIR", str(store.dir))
    auth.login(
        idp.resource,
        config=auth.load_config({"client_id": "c"}),
        announce=lambda prompt: _play_browser(prompt, idp),
        store=store,
    )

    from mcp2cli import client as c

    c._session_cache.clear()
    idp.require_bearer = True
    tools = c.fetch_tool_list(idp.resource, store.dir / "tools", 3600)
    assert tools == []
    sent = [r for r in idp.requests if r[0] == "POST" and r[1] == RESOURCE_PATH]
    assert sent, "the resource should have been called"
    headers = sent[-1][2]
    assert headers.get("authorization") == "Bearer access-1", (
        f"the initialize POST must carry the stored bearer; got {headers}"
    )


def test_client_raises_auth_challenge_without_grant(idp, store, monkeypatch):
    """No grant: the client raises the actionable challenge, not a 401 traceback."""
    monkeypatch.setenv("MCP2CLI_OAUTH_CACHE_DIR", str(store.dir))

    from mcp2cli import client as c

    c._session_cache.clear()
    with pytest.raises(auth.AuthChallenge, match="auth login"):
        c.fetch_tool_list(idp.resource, store.dir / "tools", 3600)


def test_auth_challenge_is_catchable_by_the_cli(idp, store, monkeypatch):
    """The CLI turns an AuthChallenge into exit code 2 and a two-line message."""
    monkeypatch.setenv("MCP2CLI_OAUTH_CACHE_DIR", str(store.dir))

    from mcp2cli import cli

    code = cli.main(
        ["--endpoint", idp.resource, "--cache-dir", str(store.dir / "tools"), "list-servers"]
    )
    assert code == 2


# -- the callback listener must never wedge on a held connection ----------------


def test_callback_listener_releases_connection_and_serves_another_client():
    """A browser holding its socket must not block a second client.

    Observed for real: the browser got the success page and kept the connection
    open. The listener was single-threaded and, without Connection: close, its
    one handler parked in readline on that socket - so the operator's follow-up
    request (a fresh connection) was accepted by the backlog and never answered.
    The login then appeared to hang even though the code had arrived.
    """
    import socket
    import time

    listener = auth.CallbackListener("127.0.0.1", 0, "/callback")
    port = listener.port
    path = "/callback?code=abc&state=st"

    def raw_request():
        sock = socket.create_connection(("127.0.0.1", port), timeout=4)
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: */*\r\n\r\n".encode()
        )
        return sock, sock.recv(200)

    try:
        # The browser: gets its answer and holds the socket open.
        sock_a, reply_a = raw_request()
        assert b"200 OK" in reply_a
        time.sleep(0.2)

        # A second, fresh client must still be served.
        sock_b, reply_b = raw_request()
        assert b"200 OK" in reply_b
        sock_b.close()
        sock_a.close()

        assert listener.wait(5)["code"] == "abc"
    finally:
        listener.close()


def test_callback_listener_still_captures_on_repeat_requests():
    """Re-pasting the callback URL must not upset the flow."""
    import httpx

    listener = auth.CallbackListener("127.0.0.1", 0, "/callback")
    try:
        url = f"http://127.0.0.1:{listener.port}/callback?code=abc&state=st"
        assert httpx.get(url, timeout=5).status_code == 200
        assert httpx.get(url, timeout=5).status_code == 200
        assert listener.wait(5) == {"code": "abc", "state": "st"}
    finally:
        listener.close()


def test_callback_listener_ignores_duplicate_and_truncated_callbacks():
    """First callback wins; a later or truncated one must not clobber it.

    Anton's manual curl arrived after the browser's callback carrying only
    `state=` (his shell had split the URL on `&`). Pre-fix, any second request
    overwrote the captured parameters - destroying the real code.
    """
    import httpx

    listener = auth.CallbackListener("127.0.0.1", 0, "/callback")
    try:
        base = f"http://127.0.0.1:{listener.port}/callback"
        # The real callback, complete.
        assert httpx.get(f"{base}?code=GOOD&state=st", timeout=5).status_code == 200
        # A duplicate / hand-pasted, truncated one.
        assert httpx.get(f"{base}?state=st", timeout=5).status_code == 200
        assert listener.wait(5) == {"code": "GOOD", "state": "st"}
    finally:
        listener.close()
