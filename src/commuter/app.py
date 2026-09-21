"""Local web application for Strava account authorization."""

from __future__ import annotations

import secrets
from html import escape
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from commuter.auth import SessionCodec, TokenManager
from commuter.config import Settings
from commuter.store import CredentialStore
from commuter.strava import StravaAPIError, StravaClient

REQUIRED_SCOPES = {"activity:read_all", "activity:write"}
OAUTH_STATE_COOKIE = "commuter_oauth_state"
SESSION_COOKIE = "commuter_session"
CSRF_COOKIE = "commuter_csrf"


def create_app(settings: Settings | None = None, strava_client: StravaClient | None = None) -> FastAPI:
    """Create the local OAuth application with explicit dependency injection."""

    settings = settings or Settings.from_environment()
    store = CredentialStore(settings.database_path)
    strava_client = strava_client or StravaClient(settings)
    session_codec = SessionCodec(settings.strava_client_secret)

    app = FastAPI(title="Commuter", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.strava_client = strava_client
    app.state.token_manager = TokenManager(store=store, strava_client=strava_client)
    app.state.session_codec = session_codec

    @app.get("/", response_class=HTMLResponse)
    async def home() -> str:
        return _page(
            "Commuter",
            """
            <h1>Commuter</h1>
            <p>Connect your Strava account to enable local commuter automation.</p>
            <p><a href="/auth/strava">Connect with Strava</a></p>
            <p class="note">Use Strava's official Connect button asset before deploying beyond local development.</p>
            """,
        )

    @app.get("/auth/strava")
    async def begin_strava_authorization(request: Request) -> RedirectResponse:
        canonical_origin = settings.base_url.rstrip("/")
        request_origin = str(request.base_url).rstrip("/")
        if request_origin != canonical_origin:
            return RedirectResponse(
                f"{canonical_origin}/auth/strava",
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            )

        oauth_state = secrets.token_urlsafe(32)
        query = urlencode(
            {
                "client_id": settings.strava_client_id,
                "redirect_uri": settings.callback_url,
                "response_type": "code",
                "approval_prompt": "auto",
                "scope": ",".join(sorted(REQUIRED_SCOPES)),
                "state": oauth_state,
            }
        )
        response = RedirectResponse(f"{settings.strava_authorize_url}?{query}", status_code=status.HTTP_303_SEE_OTHER)
        _set_cookie(response, settings, OAUTH_STATE_COOKIE, oauth_state, max_age=600, httponly=True)
        return response

    @app.get("/auth/strava/callback")
    async def complete_strava_authorization(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        scope: str | None = None,
        error: str | None = None,
    ) -> Response:
        expected_state = request.cookies.get(OAUTH_STATE_COOKIE)
        if not state or not expected_state or not secrets.compare_digest(state, expected_state):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth state")
        if error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Strava authorization was not completed")
        if not code:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Strava authorization code")

        granted_scopes = _parse_scopes(scope)
        if not REQUIRED_SCOPES.issubset(granted_scopes):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Required Strava permissions were not granted",
            )

        try:
            token_set = await strava_client.exchange_authorization_code(code)
        except StravaAPIError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Strava could not complete authorization",
            ) from exc
        if token_set.athlete is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Strava did not identify the connected athlete",
            )

        store.save_account(token_set.athlete, granted_scopes, token_set)
        response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(OAUTH_STATE_COOKIE)
        _set_cookie(response, settings, SESSION_COOKIE, session_codec.encode(token_set.athlete.id), max_age=604800)
        _set_cookie(response, settings, CSRF_COOKIE, secrets.token_urlsafe(32), max_age=604800, httponly=False)
        return response

    @app.get("/auth/strava/status")
    async def strava_status(request: Request) -> dict[str, object]:
        account = _current_account(request, store, session_codec)
        return {
            "athlete_id": account.athlete.id,
            "connected": True,
            "scopes": sorted(account.scopes),
        }

    @app.post("/auth/strava/disconnect", status_code=status.HTTP_204_NO_CONTENT)
    async def disconnect_strava(request: Request) -> Response:
        account = _current_account(request, store, session_codec)
        _require_csrf(request)
        try:
            await strava_client.revoke(account.refresh_token)
        except StravaAPIError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Strava could not revoke the connection; local credentials were retained",
            ) from exc
        store.delete_account(account.athlete.id)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(CSRF_COOKIE)
        return response

    @app.get("/privacy", response_class=HTMLResponse)
    async def privacy() -> str:
        return _page(
            "Privacy",
            """
            <h1>Privacy</h1>
            <p>Commuter stores only the information needed to operate its local Strava connection: OAuth credentials and settings you enter.</p>
            <p>When it updates a commute, Commuter sends the Strava activity link plus fuel-savings and CO₂-avoidance summaries to the owner-configured Discord channel.</p>
            <p>Activity data is processed for automation and must not be retained beyond the permitted short-lived window.</p>
            <p>You can disconnect Strava and request deletion through <a href="/data-deletion">data deletion</a>.</p>
            """,
        )

    @app.get("/terms", response_class=HTMLResponse)
    async def terms() -> str:
        return _page(
            "Terms",
            """
            <h1>Terms</h1>
            <p>Commuter is an independent personal tool, is not affiliated with Strava, and provides estimates only.</p>
            <p>Use is subject to Strava's applicable terms and API policies.</p>
            """,
        )

    @app.get("/support", response_class=HTMLResponse)
    async def support() -> str:
        return _page(
            "Support",
            """
            <h1>Support</h1>
            <p>This local-first service has not yet configured a public support contact. Add one before any non-personal deployment.</p>
            """,
        )

    @app.get("/data-deletion", response_class=HTMLResponse)
    async def data_deletion() -> str:
        return _page(
            "Delete connected data",
            """
            <h1>Delete connected data</h1>
            <p>After you sign in, send a POST request to <code>/auth/strava/disconnect</code> with the current CSRF token to revoke Strava access and delete local credentials.</p>
            <p>The production interface will provide a confirmed deletion form and a support contact.</p>
            """,
        )

    return app


def _set_cookie(
    response: Response,
    settings: Settings,
    name: str,
    value: str,
    max_age: int,
    httponly: bool = True,
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        httponly=httponly,
        secure=settings.secure_cookies,
        samesite="lax",
    )


def _parse_scopes(raw_scopes: str | None) -> set[str]:
    if not raw_scopes:
        return set()
    return {scope.strip() for scope in raw_scopes.replace(" ", ",").split(",") if scope.strip()}


def _current_account(request: Request, store: CredentialStore, session_codec: SessionCodec):
    athlete_id = session_codec.decode(request.cookies.get(SESSION_COOKIE))
    if athlete_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No connected Strava session")
    account = store.get_account(athlete_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No connected Strava session")
    return account


def _require_csrf(request: Request) -> None:
    header_value = request.headers.get("X-CSRF-Token")
    cookie_value = request.cookies.get(CSRF_COOKIE)
    if not header_value or not cookie_value or not secrets.compare_digest(header_value, cookie_value):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(title)} · Commuter</title>
    <style>body{{font-family:system-ui,sans-serif;line-height:1.5;max-width:44rem;margin:3rem auto;padding:0 1rem}}.note{{color:#555}}</style>
  </head>
  <body>{body}</body>
</html>"""
