"""OAuth connector for the remote MCP endpoint (Claude / ChatGPT).

Full flow on a bare FastAPI app (no torch): discovery, dynamic registration,
operator approval, PKCE exchange, refresh rotation and revocation, plus the
auth-core rule that a connector token proves something on ``/mcp`` only.
The gate-level checks that import ``main`` run where torch is installed (CI).
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import secrets
import stat
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def oauth(tmp_path, monkeypatch):
    config = importlib.import_module("core.config")
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OMNIVOICE_PUBLIC_URL", "https://voice.example.com")
    return importlib.import_module("services.mcp_oauth")


@pytest.fixture
def client(oauth):
    from api.dependencies import require_admin
    from api.routers import mcp_oauth as router_module

    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app, follow_redirects=False)


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


def _register(client, redirect=REDIRECT) -> str:
    res = client.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [redirect]})
    assert res.status_code == 201, res.text
    return res.json()["client_id"]


def _authorize(client, client_id, challenge, redirect=REDIRECT, **extra):
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "st-1",
        "scope": "mcp",
        **extra,
    }
    return client.get("/oauth/authorize", params=params)


def _approved_code(client, client_id, challenge) -> str:
    res = _authorize(client, client_id, challenge)
    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith("https://voice.example.com/#/mcp-authorize?req=")
    request_id = location.split("req=", 1)[1]

    pending = client.get(f"/api/mcp/oauth/requests/{request_id}").json()
    assert pending["client_name"] == "Claude"
    assert pending["redirect_host"] == "claude.ai"

    decided = client.post(f"/api/mcp/oauth/requests/{request_id}/decision", json={"approve": True})
    target = urlsplit(decided.json()["redirect_url"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == REDIRECT
    query = parse_qs(target.query)
    assert query["state"] == ["st-1"]
    return query["code"][0]


def _exchange(client, client_id, code, verifier):
    return client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_verifier": verifier,
    })


def test_discovery_points_at_the_public_url(client):
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == "https://voice.example.com/mcp"
    assert resource["authorization_servers"] == ["https://voice.example.com"]

    server = client.get("/.well-known/oauth-authorization-server").json()
    assert server["issuer"] == "https://voice.example.com"
    assert server["token_endpoint"] == "https://voice.example.com/oauth/token"
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert server["token_endpoint_auth_methods_supported"] == ["none"]


@pytest.mark.parametrize("uri", [
    "https://evil.example.com/cb",
    "http://claude.ai/cb",
    "https://claude.ai.evil.com/cb",
    "https://user@claude.ai/cb",
    "http://localhost:3000/cb",
])
def test_registration_refuses_foreign_redirects(client, uri):
    res = client.post("/oauth/register", json={"client_name": "x", "redirect_uris": [uri]})
    assert res.status_code == 400
    assert res.json()["error"] == "invalid_redirect_uri"


def test_chatgpt_redirect_is_accepted(client):
    _register(client, "https://chatgpt.com/connector_platform_oauth_redirect")


def test_full_flow_with_refresh_rotation_and_revocation(client, oauth):
    client_id = _register(client)
    verifier, challenge = _pkce()
    code = _approved_code(client, client_id, challenge)

    tokens = _exchange(client, client_id, code, verifier).json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["access_token"].startswith("vsat_")
    assert oauth.verify_access_token(tokens["access_token"]) == client_id

    # The code is single-use.
    assert _exchange(client, client_id, code, verifier).json()["error"] == "invalid_grant"

    rotated = client.post("/oauth/token", data={
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": client_id,
    }).json()
    assert rotated["access_token"] != tokens["access_token"]
    # Rotation retires the previous pair.
    assert oauth.verify_access_token(tokens["access_token"]) is None
    reused = client.post("/oauth/token", data={
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": client_id,
    })
    assert reused.json()["error"] == "invalid_grant"

    listed = client.get("/api/mcp/oauth/connections").json()
    assert [row["client_id"] for row in listed] == [client_id]

    assert client.post("/oauth/revoke", data={"token": rotated["refresh_token"]}).status_code == 200
    assert oauth.verify_access_token(rotated["access_token"]) is None
    assert client.get("/api/mcp/oauth/connections").json() == []


def test_wrong_pkce_verifier_fails_and_burns_the_code(client):
    client_id = _register(client)
    verifier, challenge = _pkce()
    code = _approved_code(client, client_id, challenge)

    assert _exchange(client, client_id, code, "x" * 50).json()["error"] == "invalid_grant"
    assert _exchange(client, client_id, code, verifier).json()["error"] == "invalid_grant"


def test_denial_and_unknown_clients(client):
    client_id = _register(client)
    _, challenge = _pkce()
    request_id = _authorize(client, client_id, challenge).headers["location"].split("req=", 1)[1]
    denied = client.post(f"/api/mcp/oauth/requests/{request_id}/decision", json={"approve": False})
    query = parse_qs(urlsplit(denied.json()["redirect_url"]).query)
    assert query["error"] == ["access_denied"]
    assert client.get(f"/api/mcp/oauth/requests/{request_id}").status_code == 404

    # Unknown client or unregistered redirect: an error page, never a redirect.
    assert _authorize(client, "vsc_nope", challenge).status_code == 400
    assert _authorize(client, client_id, challenge, redirect="https://claude.ai/other").status_code == 400

    # Missing PKCE goes back to the (validated) redirect with an error.
    no_pkce = _authorize(client, client_id, "", code_challenge_method="plain")
    assert no_pkce.status_code == 302
    assert parse_qs(urlsplit(no_pkce.headers["location"]).query)["error"] == ["invalid_request"]


def test_revoking_a_connection_kills_its_tokens(client, oauth):
    client_id = _register(client)
    verifier, challenge = _pkce()
    tokens = _exchange(client, client_id, _approved_code(client, client_id, challenge), verifier).json()

    assert client.delete(f"/api/mcp/oauth/connections/{client_id}").status_code == 200
    assert oauth.verify_access_token(tokens["access_token"]) is None
    assert client.delete(f"/api/mcp/oauth/connections/{client_id}").status_code == 404


def test_state_file_holds_hashes_only_and_is_private(client, oauth):
    client_id = _register(client)
    verifier, challenge = _pkce()
    tokens = _exchange(client, client_id, _approved_code(client, client_id, challenge), verifier).json()

    path = oauth._state_path()
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    assert tokens["access_token"] not in raw
    assert tokens["refresh_token"] not in raw
    assert json.loads(raw)["clients"][client_id]["redirect_uris"] == [REDIRECT]
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_admin_routes_require_admin(oauth):
    from api.routers import mcp_oauth as router_module

    app = FastAPI()
    app.include_router(router_module.router)
    remote = TestClient(app, client=("203.0.113.9", 4000))
    assert remote.get("/api/mcp/oauth/connections").status_code == 403
    assert remote.get("/.well-known/oauth-authorization-server").status_code == 200


def test_admin_routes_stay_out_of_the_openapi_contract(oauth):
    from api.routers import mcp_oauth as router_module

    app = FastAPI()
    app.include_router(router_module.router)
    assert not any("oauth" in path for path in app.openapi()["paths"])


class _Conn:
    def __init__(self, path: str, token: str, host: str = "203.0.113.9"):
        self.app = SimpleNamespace(state=SimpleNamespace(network_share=None))
        self.client = SimpleNamespace(host=host)
        self.headers = {"authorization": f"Bearer {token}"}
        self.query_params = {}
        self.cookies = {}
        self.scope = {"type": "http", "path": path, "root_path": "", "state": {}, "client": (host, 1)}


def test_connector_token_is_scoped_to_mcp(client, oauth, monkeypatch):
    auth = importlib.import_module("core.auth")
    sessions = importlib.import_module("services.admin_sessions")
    monkeypatch.setenv("OMNIVOICE_API_KEY", "MASTER_KEY_UNUSED_HERE_123")
    store = sessions.AdminSessionStore(pepper=b"x" * 32)

    client_id = _register(client)
    verifier, challenge = _pkce()
    access = _exchange(client, client_id, _approved_code(client, client_id, challenge), verifier).json()[
        "access_token"
    ]

    for path in ("/mcp", "/mcp/"):
        principal = auth.resolve_principal(_Conn(path, access), store=store)
        assert principal.kind is auth.PrincipalKind.MCP_OAUTH
        assert principal.capabilities == auth.CONSUME_CAPABILITIES
        assert not principal.allows("admin")

    for path in ("/api/settings", "/profiles", "/mcpx"):
        principal = auth.resolve_principal(_Conn(path, access), store=store)
        assert principal.kind is auth.PrincipalKind.ANONYMOUS

    forged = auth.resolve_principal(_Conn("/mcp", "vsat_" + "A" * 43), store=store)
    assert forged.kind is auth.PrincipalKind.ANONYMOUS


# ── Gate level (imports main → torch; CI) ────────────────────────────────

def test_gate_challenges_and_admits_mcp(monkeypatch, tmp_path):
    pytest.importorskip("torch")
    monkeypatch.setenv("OMNIVOICE_API_KEY", "MASTER_KEY_FOR_GATE_TEST_1234567")
    monkeypatch.setenv("OMNIVOICE_PUBLIC_URL", "https://voice.example.com")
    import main

    remote = TestClient(main.app, client=("203.0.113.9", 4000), follow_redirects=False)
    res = remote.post("/mcp", json={})
    assert res.status_code == 401
    assert 'resource_metadata="https://voice.example.com/.well-known/oauth-protected-resource/mcp"' in (
        res.headers["www-authenticate"]
    )
    # The OAuth endpoints themselves are reachable without a credential, and
    # a browser Origin does not trip the cross-site guard on them.
    assert remote.get("/.well-known/oauth-protected-resource/mcp").status_code == 200
    reg = remote.post(
        "/oauth/register",
        json={"client_name": "Claude", "redirect_uris": [REDIRECT]},
        headers={"origin": "https://claude.ai"},
    )
    assert reg.status_code == 201
    # Admin approval still needs the operator.
    assert remote.get("/api/mcp/oauth/connections").status_code == 401
