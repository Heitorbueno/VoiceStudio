"""OAuth endpoints for the remote MCP connector (Claude / ChatGPT).

Public (no credential, listed in ``main._is_public_path``):
    GET  /.well-known/oauth-protected-resource[/mcp]   RFC 9728
    GET  /.well-known/oauth-authorization-server[/mcp] RFC 8414
    POST /oauth/register                               RFC 7591
    GET  /oauth/authorize                              → in-app approval page
    POST /oauth/token                                  code / refresh_token
    POST /oauth/revoke                                 RFC 7009

Admin (operator approval and connection management):
    GET    /api/mcp/oauth/info
    GET    /api/mcp/oauth/requests/{id}
    POST   /api/mcp/oauth/requests/{id}/decision
    GET    /api/mcp/oauth/connections
    DELETE /api/mcp/oauth/connections/{client_id}
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from services import mcp_oauth

from api.dependencies import require_admin

router = APIRouter(tags=["mcp-oauth"])
admin_router = APIRouter(
    prefix="/api/mcp/oauth",
    tags=["mcp-oauth"],
    dependencies=[Depends(require_admin)],
    # Desktop/web UI only: kept out of the mobile OpenAPI contract.
    include_in_schema=False,
)

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

#: Hash route of the in-app approval page (electron renderer).
CONSENT_ROUTE = "/#/mcp-authorize"


def public_base_url(request: Request) -> str:
    """Externally visible origin: OMNIVOICE_PUBLIC_URL, else the request's own.

    Behind Cloudflare Tunnel / Caddy uvicorn already rewrites scheme and
    client from the trusted proxy headers, so the request base is correct
    there too; the env var pins it against Host-header games.
    """
    configured = os.environ.get("OMNIVOICE_PUBLIC_URL", "").strip().rstrip("/")
    if configured:
        parts = urlsplit(configured)
        if parts.scheme in {"http", "https"} and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return str(request.base_url).rstrip("/")


def resource_metadata_url(request: Request) -> str:
    return f"{public_base_url(request)}/.well-known/oauth-protected-resource/mcp"


def _oauth_error(err: mcp_oauth.OAuthError) -> JSONResponse:
    return JSONResponse(
        {"error": err.error, "error_description": err.description},
        status_code=err.status,
        headers=_NO_STORE,
    )


def _protected_resource(request: Request) -> dict:
    base = public_base_url(request)
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": [mcp_oauth.SCOPE],
        "resource_name": "VoiceStudio",
    }


def _authorization_server(request: Request) -> dict:
    base = public_base_url(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [mcp_oauth.SCOPE],
    }


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
def protected_resource(request: Request):
    return _protected_resource(request)


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
@router.get("/.well-known/oauth-authorization-server/mcp", include_in_schema=False)
def authorization_server(request: Request):
    return _authorization_server(request)


@router.post("/oauth/register", include_in_schema=False)
async def register(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _oauth_error(mcp_oauth.OAuthError("invalid_client_metadata", "JSON body required."))
    method = body.get("token_endpoint_auth_method", "none")
    if method != "none":
        return _oauth_error(mcp_oauth.OAuthError(
            "invalid_client_metadata", "Only public clients (token_endpoint_auth_method=none)."
        ))
    try:
        record = mcp_oauth.register_client(body.get("client_name"), body.get("redirect_uris"))
    except mcp_oauth.OAuthError as err:
        return _oauth_error(err)
    return JSONResponse(record, status_code=201, headers=_NO_STORE)


@router.get("/oauth/authorize", include_in_schema=False)
def authorize(request: Request):
    params = dict(request.query_params)
    try:
        request_id = mcp_oauth.start_authorization(params)
    except mcp_oauth.RedirectableError as err:
        return RedirectResponse(err.location(), status_code=302)
    except mcp_oauth.OAuthError as err:
        # Unknown client or redirect_uri: never redirect to an unvalidated URI.
        return _oauth_error(err)
    return RedirectResponse(
        f"{public_base_url(request)}{CONSENT_ROUTE}?req={request_id}",
        status_code=302,
    )


async def _form(request: Request) -> dict:
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 - malformed body is a client error
        return {}
    return {k: str(v) for k, v in form.items()}


@router.post("/oauth/token", include_in_schema=False)
async def token(request: Request):
    form = await _form(request)
    grant = form.get("grant_type")
    try:
        if grant == "authorization_code":
            tokens = mcp_oauth.exchange_code(
                form.get("code", ""),
                form.get("client_id", ""),
                form.get("redirect_uri", ""),
                form.get("code_verifier", ""),
            )
        elif grant == "refresh_token":
            tokens = mcp_oauth.refresh(form.get("refresh_token", ""), form.get("client_id", ""))
        else:
            raise mcp_oauth.OAuthError("unsupported_grant_type", "Unsupported grant_type.")
    except mcp_oauth.OAuthError as err:
        return _oauth_error(err)
    return JSONResponse(tokens, headers=_NO_STORE)


@router.post("/oauth/revoke", include_in_schema=False)
async def revoke(request: Request):
    form = await _form(request)
    mcp_oauth.revoke_token(form.get("token", ""))
    return JSONResponse({}, headers=_NO_STORE)


class _Decision(BaseModel):
    approve: bool


@admin_router.get("/info")
def info(request: Request):
    return {"mcp_url": f"{public_base_url(request)}/mcp"}


@admin_router.get("/requests/{request_id}")
def get_request(request_id: str):
    pending = mcp_oauth.get_pending(request_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Authorization request expired.")
    return {
        "id": pending.id,
        "client_name": pending.client_name,
        "redirect_host": urlsplit(pending.redirect_uri).hostname or "",
        "scope": pending.scope,
    }


@admin_router.post("/requests/{request_id}/decision")
def decision(request_id: str, body: _Decision):
    try:
        return {"redirect_url": mcp_oauth.decide(request_id, body.approve)}
    except mcp_oauth.OAuthError as err:
        raise HTTPException(status_code=err.status, detail=err.description) from err


@admin_router.get("/connections")
def connections():
    return mcp_oauth.list_connections()


@admin_router.delete("/connections/{client_id}")
def revoke_connection(client_id: str):
    if not mcp_oauth.revoke_client(client_id):
        raise HTTPException(status_code=404, detail="Connection not found.")
    return {"ok": True}


# main.py registers one ``router`` per module; the admin routes keep their
# own require_admin dependency inside it.
router.include_router(admin_router)
