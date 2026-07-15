from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ConfigDict, Field

from .accounts import AccountsClient, AccountUser, TokenPair
from .managed_keys import ManagedKeyRepository

PACKAGE_DIR = Path(__file__).resolve().parent
templates = Environment(
    loader=FileSystemLoader(PACKAGE_DIR / "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)
router = APIRouter()

ACCESS_COOKIE = "osapi_access"
REFRESH_COOKIE = "osapi_refresh"
PKCE_COOKIE = "osapi_pkce"
STATE_COOKIE = "osapi_state"


class KeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    expires_in_days: int | None = Field(default=30, ge=1, le=365)
    spend_limit_usd: str | None = None


@dataclass(frozen=True)
class Session:
    user: AccountUser
    balance: dict[str, Any] | None
    rotated: TokenPair | None = None


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(request: Request) -> Response:
    return _html("index.html", request=request)


@router.get("/docs", response_class=HTMLResponse, include_in_schema=False)
async def docs(request: Request) -> Response:
    config = request.app.state.config
    return _html("docs.html", request=request, models=list(config.models.values()))


@router.get("/verify", response_class=HTMLResponse, include_in_schema=False)
async def verify(request: Request) -> Response:
    return _html("verify.html", request=request)


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> Response:
    session = await _session(request)
    if not session:
        return RedirectResponse("/auth/start", status_code=303)
    response = _html(
        "dashboard.html",
        request=request,
        user=session.user,
        balance=session.balance,
    )
    if session.rotated:
        _set_session_cookies(response, session.rotated)
    return response


@router.get("/auth/start", include_in_schema=False)
async def auth_start(request: Request) -> Response:
    accounts: AccountsClient = request.app.state.accounts
    if not accounts.login_enabled:
        return _html(
            "error.html",
            request=request,
            title="Login is not configured",
            message="OpenSoftware Accounts registration is still being completed.",
            status_code=503,
        )
    verifier = _base64url(secrets.token_bytes(32))
    state = _base64url(secrets.token_bytes(24))
    challenge = _base64url(hashlib.sha256(verifier.encode()).digest())
    redirect_uri = f"{_app_origin()}/auth/callback"
    query = urlencode(
        {
            "client_id": accounts.client_id,
            "redirect_uri": redirect_uri,
            "scope": "profile:read billing:read credits:spend",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    response = RedirectResponse(f"{accounts.site_url}/login?{query}", status_code=303)
    cookie_options = _cookie_options(max_age=600)
    response.set_cookie(PKCE_COOKIE, verifier, **cookie_options)
    response.set_cookie(STATE_COOKIE, state, **cookie_options)
    return response


@router.get("/auth/callback", include_in_schema=False)
async def auth_callback(request: Request, code: str = "", state: str = "") -> Response:
    verifier = request.cookies.get(PKCE_COOKIE)
    expected_state = request.cookies.get(STATE_COOKIE)
    valid_state = bool(expected_state) and secrets.compare_digest(state, expected_state)
    if not code or not verifier or not valid_state:
        response = RedirectResponse("/?auth=failed", status_code=303)
    else:
        accounts: AccountsClient = request.app.state.accounts
        token = await accounts.exchange_code(
            code,
            verifier,
            f"{_app_origin()}/auth/callback",
        )
        if token:
            response = RedirectResponse("/dashboard", status_code=303)
            _set_session_cookies(response, token)
        else:
            response = RedirectResponse("/?auth=failed", status_code=303)
    response.delete_cookie(PKCE_COOKIE, path="/")
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


@router.post("/auth/logout", include_in_schema=False)
async def auth_logout(request: Request) -> Response:
    if not _same_origin(request):
        return JSONResponse({"error": "invalid_origin"}, status_code=403)
    refresh = request.cookies.get(REFRESH_COOKIE)
    if refresh:
        accounts: AccountsClient = request.app.state.accounts
        await accounts.logout(refresh)
    response = JSONResponse({"ok": True})
    _clear_session_cookies(response)
    return response


@router.get("/api/account", include_in_schema=False)
async def api_account(request: Request) -> Response:
    session = await _session(request)
    if not session:
        return JSONResponse({"error": "authentication_required"}, status_code=401)
    response = JSONResponse(
        {
            "user": {
                "id": session.user.id,
                "handle": session.user.handle,
                "email": session.user.email,
                "display_name": session.user.display_name,
                "avatar_url": session.user.avatar_url,
            },
            "balance": session.balance,
        }
    )
    if session.rotated:
        _set_session_cookies(response, session.rotated)
    return response


@router.get("/api/keys", include_in_schema=False)
async def api_keys(request: Request) -> Response:
    session = await _session(request)
    repository = _repository(request)
    if not session:
        return JSONResponse({"error": "authentication_required"}, status_code=401)
    if not repository:
        return JSONResponse({"error": "key_management_unavailable"}, status_code=503)
    records = await repository.list_keys(session.user.id)
    response = JSONResponse({"data": [record.public_dict() for record in records]})
    if session.rotated:
        _set_session_cookies(response, session.rotated)
    return response


@router.post("/api/keys", include_in_schema=False)
async def api_create_key(request: Request, body: KeyCreateRequest) -> Response:
    if not _same_origin(request):
        return JSONResponse({"error": "invalid_origin"}, status_code=403)
    session = await _session(request)
    repository = _repository(request)
    if not session:
        return JSONResponse({"error": "authentication_required"}, status_code=401)
    if not repository:
        return JSONResponse({"error": "key_management_unavailable"}, status_code=503)
    try:
        spend_limit = _usd_to_microusd(body.spend_limit_usd)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    expires_at = (
        datetime.now(UTC) + timedelta(days=body.expires_in_days)
        if body.expires_in_days is not None
        else None
    )
    created = await repository.create_key(
        session.user.id,
        body.name.strip(),
        expires_at,
        spend_limit,
    )
    response = JSONResponse(
        {"data": {**created.record.public_dict(), "key": created.secret}},
        status_code=201,
    )
    if session.rotated:
        _set_session_cookies(response, session.rotated)
    return response


@router.delete("/api/keys/{key_id}", include_in_schema=False)
async def api_revoke_key(key_id: str, request: Request) -> Response:
    if not _same_origin(request):
        return JSONResponse({"error": "invalid_origin"}, status_code=403)
    session = await _session(request)
    repository = _repository(request)
    if not session:
        return JSONResponse({"error": "authentication_required"}, status_code=401)
    if not repository:
        return JSONResponse({"error": "key_management_unavailable"}, status_code=503)
    revoked = await repository.revoke_key(session.user.id, key_id)
    response = JSONResponse({"ok": revoked}, status_code=200 if revoked else 404)
    if session.rotated:
        _set_session_cookies(response, session.rotated)
    return response


@router.get("/api/usage", include_in_schema=False)
async def api_usage(request: Request) -> Response:
    session = await _session(request)
    repository = _repository(request)
    if not session:
        return JSONResponse({"error": "authentication_required"}, status_code=401)
    if not repository:
        return JSONResponse({"error": "key_management_unavailable"}, status_code=503)
    response = JSONResponse({"data": await repository.list_usage(session.user.id)})
    if session.rotated:
        _set_session_cookies(response, session.rotated)
    return response


@router.get("/api/audit", include_in_schema=False)
async def api_audit(request: Request, limit: int = 100) -> Response:
    session = await _session(request)
    repository = _repository(request)
    if not session:
        return JSONResponse({"error": "authentication_required"}, status_code=401)
    if not repository:
        return JSONResponse({"error": "key_management_unavailable"}, status_code=503)
    response = JSONResponse(
        {"data": await repository.list_audit_events(session.user.id, _audit_limit(limit))}
    )
    if session.rotated:
        _set_session_cookies(response, session.rotated)
    return response


@router.get("/api/audit/export", include_in_schema=False)
async def api_audit_export(request: Request, format: str = "jsonl") -> Response:
    session = await _session(request)
    repository = _repository(request)
    if not session:
        return JSONResponse({"error": "authentication_required"}, status_code=401)
    if not repository:
        return JSONResponse({"error": "key_management_unavailable"}, status_code=503)
    events = await repository.list_audit_events(session.user.id, 5000)
    export_format = format.lower()
    if export_format == "jsonl":
        content = "".join(
            f"{json.dumps(event, separators=(',', ':'), sort_keys=True)}\n" for event in events
        )
        media_type = "application/x-ndjson"
    elif export_format == "csv":
        content = _audit_csv(events)
        media_type = "text/csv"
    else:
        return JSONResponse({"error": "format_must_be_jsonl_or_csv"}, status_code=422)
    response = Response(
        content=content,
        media_type=media_type,
        headers={"content-disposition": (f'attachment; filename="os-api-audit.{export_format}"')},
    )
    if session.rotated:
        _set_session_cookies(response, session.rotated)
    return response


async def _session(request: Request) -> Session | None:
    accounts: AccountsClient = request.app.state.accounts
    access = request.cookies.get(ACCESS_COOKIE)
    refresh = request.cookies.get(REFRESH_COOKIE)
    rotated: TokenPair | None = None
    if access:
        user, error_code = await accounts.me(access)
        if user and error_code is None:
            return Session(user, await accounts.balance(access))
        if error_code != 3001:
            return None
    if not refresh:
        return None
    rotated = await accounts.refresh(refresh)
    if not rotated:
        return None
    user, _ = await accounts.me(rotated.access_token)
    if not user:
        return None
    return Session(user, await accounts.balance(rotated.access_token), rotated)


def _repository(request: Request) -> ManagedKeyRepository | None:
    return request.app.state.managed_keys


def _audit_limit(value: int) -> int:
    return max(1, min(value, 1000))


def _audit_csv(events: list[dict[str, Any]]) -> str:
    fields = [
        "id",
        "event_type",
        "occurred_at",
        "key_id",
        "key_name",
        "billing_id",
        "request_id",
        "model",
        "privacy",
        "backend",
        "status",
        "reason",
        "actual_microusd",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "expires_at",
        "spend_limit_microusd",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for event in events:
        writer.writerow({key: _safe_csv_value(event.get(key)) for key in fields})
    return output.getvalue()


def _safe_csv_value(value: Any) -> str | int:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return value
    if isinstance(value, (dict, list)):
        text = json.dumps(value, separators=(",", ":"), sort_keys=True)
    else:
        text = str(value)
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _html(template_name: str, *, status_code: int = 200, **context: Any) -> HTMLResponse:
    context.setdefault("app_origin", _app_origin())
    return HTMLResponse(
        templates.get_template(template_name).render(**context),
        status_code=status_code,
    )


def _app_origin() -> str:
    return os.getenv("APP_ORIGIN", "http://localhost:8080").rstrip("/")


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return origin == _app_origin()


def _cookie_options(*, max_age: int | None = None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "httponly": True,
        "secure": _app_origin().startswith("https://"),
        "samesite": "lax",
        "path": "/",
    }
    if max_age is not None:
        options["max_age"] = max_age
    return options


def _set_session_cookies(response: Response, token: TokenPair) -> None:
    response.set_cookie(ACCESS_COOKIE, token.access_token, **_cookie_options(max_age=3600))
    response.set_cookie(
        REFRESH_COOKIE,
        token.refresh_token,
        **_cookie_options(max_age=60 * 60 * 24 * 30),
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/")


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _usd_to_microusd(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        dollars = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("spend_limit_usd must be a dollar amount") from exc
    if dollars < Decimal("0.01") or dollars > Decimal("10000"):
        raise ValueError("spend_limit_usd must be between 0.01 and 10000")
    microusd = dollars * Decimal(1_000_000)
    if microusd != microusd.to_integral_value():
        raise ValueError("spend_limit_usd supports at most six decimal places")
    return int(microusd)
