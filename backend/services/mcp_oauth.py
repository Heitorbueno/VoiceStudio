"""OAuth 2.1 authorization server for the remote MCP connector.

Lets Claude (claude.ai / claude.com) and ChatGPT add this backend's ``/mcp``
endpoint as a custom connector without ever seeing the operator API key:

* dynamic client registration, restricted to the connector redirect hosts;
* authorization code + PKCE (S256 only), each code single-use, 60 s;
* the operator approves every connection in the app (Settings → Sharing);
* opaque access tokens (1 h) and rotating refresh tokens (30 d), RFC 7009
  revocation, and per-connection revocation from the app.

Only SHA-256 hashes of codes and tokens are stored, in
``<data dir>/mcp_oauth.json`` (0600), so a copied state file grants nothing.
An OAuth token is accepted on ``/mcp`` only, with consume capability: it
can never reach the admin API.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

ACCESS_PREFIX = "vsat_"
REFRESH_PREFIX = "vsrt_"
SCOPE = "mcp"

ACCESS_TTL_S = 3600
REFRESH_TTL_S = 30 * 86400
CODE_TTL_S = 60
PENDING_TTL_S = 600

MAX_CLIENTS = 50
MAX_PENDING = 50
MAX_REDIRECT_URIS = 5

#: Redirect hosts of the connector products this server is built for.
CONNECTOR_REDIRECT_HOSTS = frozenset({
    "claude.ai",
    "claude.com",
    "chatgpt.com",
    "chat.openai.com",
    "platform.openai.com",
})

_LOCAL_REDIRECT_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

_lock = threading.RLock()


class OAuthError(Exception):
    """An RFC 6749 error response: ``error`` code plus a human description."""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


@dataclass(frozen=True)
class PendingRequest:
    id: str
    client_id: str
    client_name: str
    redirect_uri: str
    scope: str


def _now() -> int:
    return int(time.time())


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _state_path() -> str:
    from core.config import DATA_DIR

    return os.path.join(DATA_DIR, "mcp_oauth.json")


def _empty() -> dict:
    return {"clients": {}, "pending": {}, "codes": {}, "access": {}, "refresh": {}}


def _load() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    state = _empty()
    for key in state:
        value = data.get(key)
        if isinstance(value, dict):
            state[key] = value
    return state


def _save(state: dict) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _prune(state: dict, now: int) -> None:
    for bucket in ("pending", "codes", "access", "refresh"):
        items = state[bucket]
        for key in [k for k, v in items.items() if int(v.get("exp", 0)) <= now]:
            del items[key]


def _allow_local_redirects() -> bool:
    flag = os.environ.get("OMNIVOICE_MCP_OAUTH_ALLOW_LOCALHOST", "")
    return flag.strip().lower() in {"1", "true", "yes", "on"}


def redirect_uri_allowed(uri: str) -> bool:
    """https on a connector host, or loopback http only when explicitly enabled."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment or parts.username or parts.password:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host in CONNECTOR_REDIRECT_HOSTS and parts.port in (None, 443):
        return True
    if _allow_local_redirects() and parts.scheme in {"http", "https"}:
        return host in _LOCAL_REDIRECT_HOSTS
    return False


def _clean_name(raw: object) -> str:
    name = str(raw or "").strip()
    name = "".join(ch for ch in name if ch.isprintable())[:80]
    return name or "MCP client"


def register_client(client_name: object, redirect_uris: object) -> dict:
    """RFC 7591 dynamic registration of a public (PKCE) client."""
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise OAuthError("invalid_redirect_uri", "redirect_uris is required.")
    if len(redirect_uris) > MAX_REDIRECT_URIS:
        raise OAuthError("invalid_redirect_uri", "Too many redirect_uris.")
    uris = []
    for uri in redirect_uris:
        if not isinstance(uri, str) or len(uri) > 2048 or not redirect_uri_allowed(uri):
            raise OAuthError(
                "invalid_redirect_uri",
                "Only Claude and ChatGPT connector redirect URIs are accepted.",
            )
        uris.append(uri)
    now = _now()
    client_id = "vsc_" + secrets.token_urlsafe(18)
    record = {"name": _clean_name(client_name), "redirect_uris": uris, "created_at": now}
    with _lock:
        state = _load()
        _prune(state, now)
        if len(state["clients"]) >= MAX_CLIENTS:
            # Drop the oldest client that holds no live refresh token.
            live = {r.get("client_id") for r in state["refresh"].values()}
            idle = sorted(
                (c for c in state["clients"] if c not in live),
                key=lambda c: int(state["clients"][c].get("created_at", 0)),
            )
            if not idle:
                raise OAuthError("invalid_client_metadata", "Client limit reached.", 429)
            del state["clients"][idle[0]]
        state["clients"][client_id] = record
        _save(state)
    return {
        "client_id": client_id,
        "client_id_issued_at": now,
        "client_name": record["name"],
        "redirect_uris": uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def start_authorization(params: dict) -> str:
    """Validate an /authorize request and park it for operator approval.

    Returns the pending request id. Raises OAuthError; the caller decides
    whether the error may be redirected (only once client + redirect_uri
    are known-good).
    """
    client_id = str(params.get("client_id") or "")
    redirect_uri = str(params.get("redirect_uri") or "")
    with _lock:
        state = _load()
        now = _now()
        _prune(state, now)
        client = state["clients"].get(client_id)
        if client is None:
            raise OAuthError("invalid_client", "Unknown client_id.")
        if redirect_uri not in client.get("redirect_uris", []):
            raise OAuthError("invalid_request", "redirect_uri is not registered for this client.")
        if params.get("response_type") != "code":
            raise RedirectableError(redirect_uri, params.get("state"), "unsupported_response_type",
                                    "Only response_type=code is supported.")
        challenge = str(params.get("code_challenge") or "")
        if params.get("code_challenge_method") != "S256" or not (43 <= len(challenge) <= 128):
            raise RedirectableError(redirect_uri, params.get("state"), "invalid_request",
                                    "PKCE with code_challenge_method=S256 is required.")
        scope = str(params.get("scope") or SCOPE)
        if scope.split() and set(scope.split()) - {SCOPE}:
            raise RedirectableError(redirect_uri, params.get("state"), "invalid_scope",
                                    "Only the 'mcp' scope is available.")
        if len(state["pending"]) >= MAX_PENDING:
            oldest = min(state["pending"], key=lambda k: int(state["pending"][k].get("exp", 0)))
            del state["pending"][oldest]
        request_id = secrets.token_urlsafe(12)
        state["pending"][request_id] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "state": params.get("state"),
            "scope": SCOPE,
            "resource": params.get("resource"),
            "exp": now + PENDING_TTL_S,
        }
        _save(state)
    return request_id


class RedirectableError(OAuthError):
    """An OAuth error that is delivered to the (validated) redirect_uri."""

    def __init__(self, redirect_uri: str, state: object, error: str, description: str):
        super().__init__(error, description)
        self.redirect_uri = redirect_uri
        self.state = state

    def location(self) -> str:
        return _with_query(self.redirect_uri, {
            "error": self.error,
            "error_description": self.description,
            "state": self.state,
        })


def _with_query(uri: str, values: dict) -> str:
    query = urlencode({k: v for k, v in values.items() if v is not None})
    return f"{uri}{'&' if '?' in uri else '?'}{query}"


def get_pending(request_id: str) -> PendingRequest | None:
    with _lock:
        state = _load()
        entry = state["pending"].get(request_id)
        if entry is None or int(entry.get("exp", 0)) <= _now():
            return None
        client = state["clients"].get(entry["client_id"])
        if client is None:
            return None
        return PendingRequest(
            id=request_id,
            client_id=entry["client_id"],
            client_name=str(client.get("name") or "MCP client"),
            redirect_uri=entry["redirect_uri"],
            scope=entry.get("scope") or SCOPE,
        )


def decide(request_id: str, approve: bool) -> str:
    """Resolve a pending request; returns where to send the operator's browser."""
    with _lock:
        state = _load()
        now = _now()
        _prune(state, now)
        entry = state["pending"].pop(request_id, None)
        if entry is None or entry["client_id"] not in state["clients"]:
            _save(state)
            raise OAuthError("invalid_request", "Authorization request expired.", 404)
        if not approve:
            _save(state)
            return _with_query(entry["redirect_uri"], {
                "error": "access_denied",
                "error_description": "The operator denied the connection.",
                "state": entry.get("state"),
            })
        code = secrets.token_urlsafe(32)
        state["codes"][_hash(code)] = {
            "client_id": entry["client_id"],
            "redirect_uri": entry["redirect_uri"],
            "code_challenge": entry["code_challenge"],
            "scope": entry.get("scope") or SCOPE,
            "exp": now + CODE_TTL_S,
        }
        _save(state)
    return _with_query(entry["redirect_uri"], {"code": code, "state": entry.get("state")})


def _pkce_ok(verifier: str, challenge: str) -> bool:
    if not (43 <= len(verifier) <= 128):
        return False
    digest = hashlib.sha256(verifier.encode("ascii", "ignore")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, challenge)


def _issue(state: dict, client_id: str, family: str, now: int) -> dict:
    access = ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
    state["access"][_hash(access)] = {"client_id": client_id, "family": family, "exp": now + ACCESS_TTL_S}
    state["refresh"][_hash(refresh)] = {"client_id": client_id, "family": family, "exp": now + REFRESH_TTL_S}
    client = state["clients"].get(client_id)
    if client is not None:
        client["last_used_at"] = now
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_S,
        "refresh_token": refresh,
        "scope": SCOPE,
    }


def _revoke_family(state: dict, family: str) -> None:
    for bucket in ("access", "refresh"):
        items = state[bucket]
        for key in [k for k, v in items.items() if v.get("family") == family]:
            del items[key]


def exchange_code(code: str, client_id: str, redirect_uri: str, verifier: str) -> dict:
    with _lock:
        state = _load()
        now = _now()
        _prune(state, now)
        entry = state["codes"].pop(_hash(code or ""), None)
        # The code is consumed even when the exchange fails: single use.
        _save(state)
        if (
            entry is None
            or entry["client_id"] != client_id
            or entry["redirect_uri"] != redirect_uri
            or client_id not in state["clients"]
        ):
            raise OAuthError("invalid_grant", "Invalid or expired authorization code.")
        if not _pkce_ok(verifier or "", entry["code_challenge"]):
            raise OAuthError("invalid_grant", "PKCE verification failed.")
        tokens = _issue(state, client_id, secrets.token_urlsafe(9), now)
        _save(state)
    return tokens


def refresh(refresh_token: str, client_id: str) -> dict:
    with _lock:
        state = _load()
        now = _now()
        _prune(state, now)
        entry = state["refresh"].pop(_hash(refresh_token or ""), None)
        if entry is None or entry["client_id"] != client_id or client_id not in state["clients"]:
            _save(state)
            raise OAuthError("invalid_grant", "Invalid or expired refresh token.")
        # Rotation: the old refresh token is gone; its access tokens too.
        _revoke_family(state, entry["family"])
        tokens = _issue(state, client_id, entry["family"], now)
        _save(state)
    return tokens


def revoke_token(token: str) -> None:
    """RFC 7009: revoking either token ends the whole grant. Always succeeds."""
    with _lock:
        state = _load()
        digest = _hash(token or "")
        entry = state["access"].get(digest) or state["refresh"].get(digest)
        if entry is None:
            return
        _revoke_family(state, entry["family"])
        _save(state)


def verify_access_token(token: str) -> str | None:
    """The client id an access token belongs to, or None."""
    if not token.startswith(ACCESS_PREFIX):
        return None
    with _lock:
        state = _load()
        entry = state["access"].get(_hash(token))
        if entry is None or int(entry.get("exp", 0)) <= _now():
            return None
        client_id = entry.get("client_id")
        return client_id if client_id in state["clients"] else None


def list_connections() -> list[dict]:
    """Authorized connectors (clients holding a live grant), newest first."""
    with _lock:
        state = _load()
        now = _now()
        live: dict[str, int] = {}
        for entry in state["refresh"].values():
            if int(entry.get("exp", 0)) > now:
                cid = entry.get("client_id")
                live[cid] = live.get(cid, 0) + 1
        rows = []
        for cid, client in state["clients"].items():
            if cid not in live:
                continue
            rows.append({
                "client_id": cid,
                "name": client.get("name") or "MCP client",
                "redirect_host": (urlsplit((client.get("redirect_uris") or [""])[0]).hostname or ""),
                "created_at": client.get("created_at"),
                "last_used_at": client.get("last_used_at"),
                "grants": live[cid],
            })
    rows.sort(key=lambda r: int(r.get("last_used_at") or r.get("created_at") or 0), reverse=True)
    return rows


def revoke_client(client_id: str) -> bool:
    with _lock:
        state = _load()
        if state["clients"].pop(client_id, None) is None:
            return False
        for bucket in ("access", "refresh", "codes", "pending"):
            items = state[bucket]
            for key in [k for k, v in items.items() if v.get("client_id") == client_id]:
                del items[key]
        _save(state)
    return True
