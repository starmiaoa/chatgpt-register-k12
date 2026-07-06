"""OAuth login flow with Team workspace selection.

After a child account joins the parent K12 workspace, it has TWO scopes:
  - personal (from registration)
  - team (from workspace membership)

The sub2api export MUST use team-scoped tokens. To get team-scoped
tokens, we re-run the OAuth login flow (same as registration but with
screen_hint=login instead of signup), and during the flow we select
the team workspace.

Flow:
  1. authorize?screen_hint=login&login_hint={email}
  2. POST user login (password)
  3. OTP verification (configured mail provider)
  4. Handle workspace selection → pick team
  5. Exchange code → team-scoped access_token + refresh_token + id_token

NOTE: The exact workspace selection API is confirmed at runtime by
inspecting the authorize response. See _select_team_workspace().
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from chatgpt_register_k12.register.headers import json_headers, navigate_headers
from chatgpt_register_k12.register.mail_provider import wait_for_code
from chatgpt_register_k12.register.session import (
    create_register_session,
    request_with_retry,
)
from chatgpt_register_k12.utils.pkce import generate_pkce
from chatgpt_register_k12.utils.sentinel import build_sentinel_token

# ── Constants ───────────────────────────────────────────────────────

AUTH_BASE = "https://auth.openai.com"
PLATFORM_BASE = "https://platform.openai.com"
PLATFORM_OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
PLATFORM_OAUTH_REDIRECT_URI = f"{PLATFORM_BASE}/auth/callback"
PLATFORM_OAUTH_AUDIENCE = "https://api.openai.com/v1"
PLATFORM_AUTH0_CLIENT = (
    "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

logger = logging.getLogger(__name__)


class LoginError(RuntimeError):
    """Login flow failed."""


class PasswordRequiredError(LoginError):
    """The auth session requires password login; OTP-only login is unavailable."""


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _response_detail(resp, limit: int = 500) -> str:
    if resp is None:
        return "resp=None"
    data = _response_json(resp)
    if data:
        return f"json={data}"
    try:
        return f"body={str(resp.text or '')[:limit]}"
    except Exception:
        return "body=(unavailable)"


def _extract_code_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = parse_qs(urlparse(url).query)
    except Exception:
        return ""
    return str((parsed.get("code") or [""])[0]).strip()


def _normalise_possible_url(value: str) -> str:
    value = unescape(str(value or "").strip())
    value = value.replace("\\u0026", "&").replace("\\/", "/")
    for _ in range(2):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


def _extract_code_from_text(text: str) -> str:
    """Find an OAuth callback URL embedded in HTML/JS without logging it."""
    if not text:
        return ""

    text = _normalise_possible_url(text)
    patterns = [
        r"https://platform\.openai\.com/auth/callback[^\"'<>\s]*",
        r"https%3A%2F%2Fplatform\.openai\.com%2Fauth%2Fcallback[^\"'<>\s]*",
        r"/auth/callback\?[^\"'<>\s]*",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            url = _normalise_possible_url(match.group(0))
            code = _extract_code_from_url(url)
            if code:
                return code
    return ""


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _page_type_from_data(data: dict[str, Any]) -> str:
    page = data.get("page")
    if isinstance(page, dict):
        return str(page.get("type") or "").strip()
    return ""


def _extract_code_from_data(data: dict[str, Any]) -> str:
    for value in _iter_strings(data):
        text = _normalise_possible_url(value)
        code = _extract_code_from_url(text) or _extract_code_from_text(text)
        if code:
            return code
    return ""


def _iter_response_urls(resp):
    if resp is None:
        return
    history = list(getattr(resp, "history", []) or [])
    for item in [*history, resp]:
        url = str(getattr(item, "url", "") or "").strip()
        if url:
            yield url
        location = str(getattr(item, "headers", {}).get("Location", "") or "").strip()
        if location:
            yield location


def _extract_code_from_response(resp, data: dict[str, Any] | None = None) -> str:
    for url in _iter_response_urls(resp):
        code = _extract_code_from_url(_normalise_possible_url(url))
        if code:
            return code
    if data:
        code = _extract_code_from_data(data)
        if code:
            return code
    try:
        body = str(getattr(resp, "text", "") or "")
    except Exception:
        body = ""
    return _extract_code_from_text(body)


def _safe_url_shape(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return "(unparseable)"
    query_keys = ",".join(sorted(parse_qs(parsed.query).keys()))
    suffix = f"?{query_keys}" if query_keys else ""
    return f"{parsed.netloc}{parsed.path}{suffix}"


def _response_shape(resp) -> str:
    if resp is None:
        return "resp=None"
    urls = [_safe_url_shape(url) for url in _iter_response_urls(resp)]
    urls = [url for url in urls if url]
    content_type = str(getattr(resp, "headers", {}).get("content-type", "") or "")
    return (
        f"final={_safe_url_shape(str(getattr(resp, 'url', '') or '')) or '(none)'}, "
        f"history={len(getattr(resp, 'history', []) or [])}, "
        f"locations={urls[:4]}, "
        f"content_type={content_type.split(';', 1)[0] or '(none)'}"
    )


def _page_type_from_response_url(resp) -> str:
    """Infer auth page type from an HTML navigation response URL."""
    final_url = str(getattr(resp, "url", "") or "").strip()
    try:
        parsed = urlparse(final_url)
    except Exception:
        return ""

    path = parsed.path.rstrip("/")
    if path.endswith("/log-in/password"):
        return "login_password"
    if path.endswith("/email-verification"):
        return "email_otp_verification"
    if path.endswith("/workspace"):
        return "workspace"
    if path.endswith("/about-you"):
        return "about_you"
    if "/sign-in-with-chatgpt" in path and "consent" in path:
        return "sign_in_with_chatgpt_consent"
    return ""


def _data_with_response_hints(resp) -> dict[str, Any]:
    """Merge JSON response data with redirect/code/page hints from navigation."""
    data = _response_json(resp)

    location = ""
    for url in _iter_response_urls(resp):
        if url:
            location = url
    if location and not data.get("continue_url"):
        data["continue_url"] = location

    code = _extract_code_from_response(resp, data)
    if code and not _extract_code_from_data(data):
        data["continue_url"] = f"{PLATFORM_OAUTH_REDIRECT_URI}?{urlencode({'code': code})}"

    if not _page_type_from_data(data):
        inferred_page = _page_type_from_response_url(resp)
        if inferred_page:
            data["page"] = {"type": inferred_page}
    return data


def _client_auth_session_dump(session) -> dict[str, Any]:
    resp, _ = request_with_retry(
        session,
        "get",
        f"{AUTH_BASE}/api/accounts/client_auth_session_dump",
        headers={
            "accept": "application/json",
            "user-agent": USER_AGENT,
        },
        verify=True,
    )
    if resp is None or resp.status_code != 200:
        return {}
    data = _response_json(resp)
    session_data = data.get("client_auth_session")
    return session_data if isinstance(session_data, dict) else data


def _authenticated_authorize(
    session,
    device_id: str,
    email: str,
) -> tuple[str, str, dict[str, Any]]:
    """Run authorize again after password and OTP have authenticated the session."""
    code_verifier, code_challenge = generate_pkce()
    params = {
        "issuer": AUTH_BASE,
        "client_id": PLATFORM_OAUTH_CLIENT_ID,
        "audience": PLATFORM_OAUTH_AUDIENCE,
        "redirect_uri": PLATFORM_OAUTH_REDIRECT_URI,
        "device_id": device_id,
        "screen_hint": "login",
        "login_hint": email,
        "scope": "openid profile email offline_access",
        "response_type": "code",
        "response_mode": "query",
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "auth0Client": PLATFORM_AUTH0_CLIENT,
    }
    url = f"{AUTH_BASE}/api/accounts/authorize?{urlencode(params)}"
    logger.info(f"[{email}] Team login: restarting authorize for existing account")
    resp, error = request_with_retry(
        session,
        "get",
        url,
        headers=navigate_headers(f"{PLATFORM_BASE}/"),
        allow_redirects=True,
        verify=True,
    )
    if resp is None or resp.status_code != 200:
        raise LoginError(
            f"Authenticated authorize failed: HTTP "
            f"{getattr(resp, 'status_code', '?')}, {error or ''}"
        )

    data = _data_with_response_hints(resp)
    code = _extract_code_from_response(resp, data)
    if not code and not _page_type_from_data(data):
        dump = _client_auth_session_dump(session)
        dump_code = _extract_code_from_data(dump)
        if dump_code or _page_type_from_data(dump):
            data = dump
            code = dump_code
    logger.info(
        f"[{email}] Team login: restarted authorize HTTP {resp.status_code}, "
        f"code={'yes' if code else 'no'}, "
        f"next page={_page_type_from_data(data) or '(none)'}, "
        f"{_response_shape(resp)}"
    )
    return code, code_verifier, data


def _extract_workspaces(
    session,
    response_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    if response_data:
        candidates.append(response_data.get("oai-client-auth-session"))
        candidates.append(response_data.get("client_auth_session"))
        candidates.append(response_data)
    candidates.append(_client_auth_session_dump(session))

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        workspaces = candidate.get("workspaces")
        if isinstance(workspaces, list) and workspaces:
            return [item for item in workspaces if isinstance(item, dict)]
    return []


def _default_profile(email: str) -> tuple[str, str]:
    local = str(email or "user").split("@", 1)[0]
    cleaned = "".join(ch if ch.isalpha() else " " for ch in local).strip()
    words = [word.capitalize() for word in cleaned.split() if word]
    name = " ".join(words[:2]) or "OpenAI User"
    year = 1996 + secrets.randbelow(8)
    month = 1 + secrets.randbelow(12)
    day = 1 + secrets.randbelow(28)
    return name, f"{year:04d}-{month:02d}-{day:02d}"


def _complete_about_you(
    session,
    device_id: str,
    email: str,
) -> dict[str, Any]:
    """Submit the about-you page when login lands on an incomplete profile."""
    name, birthdate = _default_profile(email)
    logger.info(f"[{email}] Team login: completing about-you profile")
    url = f"{AUTH_BASE}/api/accounts/create_account"
    headers = json_headers(f"{AUTH_BASE}/about-you", device_id)
    headers["openai-sentinel-token"] = build_sentinel_token(
        session,
        device_id,
        "oauth_create_account",
        user_agent=USER_AGENT,
    )[0]

    resp, error = request_with_retry(
        session,
        "post",
        url,
        json={"name": name, "birthdate": birthdate},
        headers=headers,
        verify=True,
        allow_redirects=False,
    )
    if resp is None:
        raise LoginError(f"about-you submit failed: {error or 'no response'}")
    data = _data_with_response_hints(resp)
    error_data = data.get("error") if isinstance(data.get("error"), dict) else {}
    if resp.status_code == 400 and error_data.get("code") == "user_already_exists":
        logger.info(f"[{email}] Team login: about-you reported existing account")
        return {
            "page": {"type": "existing_account"},
            "error": error_data,
        }
    if resp.status_code not in (200, 302, 303):
        raise LoginError(
            f"about-you submit failed: HTTP {resp.status_code}, "
            f"{_response_detail(resp)}"
        )

    logger.info(
        f"[{email}] Team login: about-you HTTP {resp.status_code}, "
        f"next page={_page_type_from_data(data) or '(none)'}"
    )
    return data


# ── Re-login with workspace selection ───────────────────────────────


def _start_login_authorize(
    session,
    device_id: str,
    email: str,
) -> tuple[dict[str, Any], str]:
    logger.info(f"[{email}] Team login: authorizing")
    code_verifier, code_challenge = generate_pkce()

    session.cookies.set("oai-did", device_id, domain=".auth.openai.com")
    session.cookies.set("oai-did", device_id, domain="auth.openai.com")

    params = {
        "issuer": AUTH_BASE,
        "client_id": PLATFORM_OAUTH_CLIENT_ID,
        "audience": PLATFORM_OAUTH_AUDIENCE,
        "redirect_uri": PLATFORM_OAUTH_REDIRECT_URI,
        "device_id": device_id,
        "screen_hint": "login",
        "max_age": "0",
        "login_hint": email,
        "scope": "openid profile email offline_access",
        "response_type": "code",
        "response_mode": "query",
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "auth0Client": PLATFORM_AUTH0_CLIENT,
    }
    auth_url = f"{AUTH_BASE}/api/accounts/authorize?{urlencode(params)}"
    headers = navigate_headers(f"{PLATFORM_BASE}/")

    resp, error = request_with_retry(
        session,
        "get",
        auth_url,
        headers=headers,
        allow_redirects=True,
        verify=True,
    )
    if resp is None or resp.status_code != 200:
        raise LoginError(
            f"Login authorize failed: HTTP "
            f"{getattr(resp, 'status_code', '?')}, {error or ''}"
        )
    logger.info(f"[{email}] Team login: authorize HTTP {resp.status_code}")
    return _data_with_response_hints(resp), code_verifier


def re_login_for_team_token(
    email: str,
    password: str,
    mail_config: dict,
    proxy: str = "",
    flaresolverr_url: str = "",
    workspace_id: str = "",
) -> dict:
    """Re-login to get a team-scoped access token.

    1. Run OAuth authorize as login (not signup)
    2. Enter password
    3. Handle OTP
    4. Navigate workspace selection → pick team
    5. Exchange code for tokens

    Args:
        email: Account email
        password: Account password
        mail_config: Mail config for OTP code retrieval
        proxy: Proxy URL
        flaresolverr_url: FlareSolverr URL
        workspace_id: K12 workspace UUID (used to identify the team)

    Returns:
        {access_token, refresh_token, id_token, email, scope: "team"}
    """
    session = create_register_session(
        proxy=proxy, flaresolverr_url=flaresolverr_url
    )
    device_id = str(uuid.uuid4())

    try:
        # Step 1: Authorize as login
        _, code_verifier = _start_login_authorize(session, device_id, email)

        # Step 2: Verify password against the current auth session.
        logger.info(f"[{email}] Team login: verifying password")
        data = _handle_password_verification(
            session,
            device_id,
            password,
        )
        logger.info(
            f"[{email}] Team login: password accepted, "
            f"next page={_page_type_from_data(data) or '(none)'}"
        )

        # Step 3+: Follow whichever continuation the auth session requires.
        code, code_verifier = _complete_login_flow(
            session=session,
            device_id=device_id,
            email=email,
            mail_config=mail_config,
            workspace_id=workspace_id,
            response_data=data,
            code_verifier=code_verifier,
            password=password,
        )
        logger.info(f"[{email}] Team login: authorization code received")

        # Final step: exchange the callback code from the original authorize.
        logger.info(f"[{email}] Team login: exchanging code for tokens")
        tokens = _exchange_login_tokens(
            session,
            code_verifier=code_verifier,
            code=code,
        )
        logger.info(f"[{email}] Team login: token exchange succeeded")

        return {
            "email": email,
            "password": password,
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "scope": "team" if workspace_id else "personal",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    finally:
        session.close()


def re_login_email_otp_for_team_token(
    email: str,
    mail_config: dict,
    proxy: str = "",
    flaresolverr_url: str = "",
    workspace_id: str = "",
) -> dict:
    """Re-login using only email OTP when the auth session allows it."""
    session = create_register_session(
        proxy=proxy, flaresolverr_url=flaresolverr_url
    )
    device_id = str(uuid.uuid4())

    try:
        data, code_verifier = _start_login_authorize(session, device_id, email)
        page_type = _page_type_from_data(data)
        code = _extract_code_from_data(data)
        if not code:
            if page_type in {"login_password", "password", ""}:
                logger.info(f"[{email}] Team login: trying email OTP login")
                try:
                    _send_login_otp(session)
                    data = {"page": {"type": "email_otp_verification"}}
                except Exception as error:
                    raise PasswordRequiredError(
                        "OTP-only login is unavailable; password is required"
                    ) from error
            elif page_type == "email_otp_send":
                _send_login_otp(session)
                data = {"page": {"type": "email_otp_verification"}}

            code, code_verifier = _complete_login_flow(
                session=session,
                device_id=device_id,
                email=email,
                mail_config=mail_config,
                workspace_id=workspace_id,
                response_data=data,
                code_verifier=code_verifier,
                password=None,
            )

        logger.info(f"[{email}] Team login: exchanging code for tokens")
        tokens = _exchange_login_tokens(
            session,
            code_verifier=code_verifier,
            code=code,
        )
        logger.info(f"[{email}] Team login: token exchange succeeded")

        return {
            "email": email,
            "password": "",
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "scope": "team" if workspace_id else "personal",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        session.close()


def _handle_password_verification(
    session,
    device_id: str,
    password: str,
) -> dict[str, Any]:
    """Submit password during login flow.

    OpenAI's current auth web app submits password verification to
    `/api/accounts/password/verify` and then returns the next auth page
    as JSON (`external_url`, `email_otp_verification`, `workspace`, etc.).
    """
    url = f"{AUTH_BASE}/api/accounts/password/verify"
    headers = json_headers(f"{AUTH_BASE}/log-in/password", device_id)
    logger.info("Team login: building password sentinel token")
    sentinel_token, _ = build_sentinel_token(
        session, device_id, "password_verify",
        user_agent=USER_AGENT,
    )
    logger.info("Team login: password sentinel token built")
    headers["openai-sentinel-token"] = sentinel_token

    resp, error = request_with_retry(
        session, "post", url,
        json={"password": password},
        headers=headers, verify=True,
    )

    if resp is None:
        raise LoginError(
            f"Password verification failed: {error or 'no response'}"
        )
    if resp.status_code not in (200, 302, 303):
        data = _response_json(resp)
        error_data = data.get("error") if isinstance(data.get("error"), dict) else {}
        detail = (
            error_data.get("message")
            or data.get("message")
            or resp.text[:200]
        )
        raise LoginError(
            f"Password verification failed (HTTP {resp.status_code}): {detail}"
        )
    return _data_with_response_hints(resp)


def _send_login_otp(session) -> None:
    """Request a login OTP email for the current auth session."""
    logger.info("Team login: requesting login OTP email")
    url = f"{AUTH_BASE}/api/accounts/email-otp/send"
    headers = navigate_headers(f"{AUTH_BASE}/email-verification")

    resp, error = request_with_retry(
        session, "get", url, headers=headers,
        allow_redirects=True, verify=True,
    )
    if resp is None or resp.status_code not in (200, 302):
        raise LoginError(
            f"Login OTP send failed: HTTP "
            f"{getattr(resp, 'status_code', '?')}"
        )
    logger.info(f"Team login: OTP send HTTP {resp.status_code}")


def _validate_login_otp(
    session,
    device_id: str,
    email: str,
    mail_config: dict,
    not_before: datetime | None = None,
) -> dict[str, Any]:
    """Wait for and validate a login OTP, returning the next auth page."""
    logger.info(f"[{email}] Team login: waiting for login OTP email")
    mailbox = {
        "address": email,
        "_code_subject_hint": "login",
    }
    if not_before is not None:
        mailbox["_code_not_before"] = not_before
    code = wait_for_code(mail_config, mailbox)
    if not code:
        raise LoginError("Timed out waiting for login OTP code")
    logger.info(f"[{email}] Team login: OTP code received")

    headers = json_headers(f"{AUTH_BASE}/email-verification", device_id)

    resp, error = request_with_retry(
        session, "post",
        f"{AUTH_BASE}/api/accounts/email-otp/validate",
        json={"code": code},
        headers=headers, verify=True,
    )
    if resp is None or resp.status_code != 200:
        # Retry with sentinel
        sentinel_token, _ = build_sentinel_token(
            session, device_id, "authorize_continue",
            user_agent=USER_AGENT,
        )
        headers["openai-sentinel-token"] = sentinel_token
        resp, error = request_with_retry(
            session, "post",
            f"{AUTH_BASE}/api/accounts/email-otp/validate",
            json={"code": code},
            headers=headers, verify=True,
        )
        if resp is None or resp.status_code != 200:
            raise LoginError(
                f"Login OTP validation failed: HTTP "
                f"{getattr(resp, 'status_code', '?')}, {_response_detail(resp)}"
            )
    logger.info(
        f"[{email}] Team login: OTP validation HTTP "
        f"{getattr(resp, 'status_code', '?')}"
    )
    return _data_with_response_hints(resp)


def _select_team_workspace(
    session,
    device_id: str,
    workspace_id: str,
    response_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select the requested workspace from the current auth session."""
    logger.info(f"Team login: selecting workspace {workspace_id}")
    if not workspace_id:
        raise LoginError("Workspace selection requested without a workspace_id")

    workspaces = _extract_workspaces(session, response_data)
    logger.info(f"Team login: auth session has {len(workspaces)} workspace(s)")
    if not workspaces:
        raise LoginError(
            f"Workspace {workspace_id} is not present in the current auth "
            "session. The join step did not create an active workspace "
            "membership."
        )

    selected = next(
        (
            item for item in workspaces
            if str(item.get("id") or item.get("workspace_id") or "").strip()
            == workspace_id
        ),
        None,
    )
    if selected is None:
        available = [
            str(item.get("id") or item.get("workspace_id") or "").strip()
            for item in workspaces
            if item.get("id") or item.get("workspace_id")
        ]
        raise LoginError(
            f"Workspace {workspace_id} not found in auth session workspaces: "
            f"{available}"
        )

    url = f"{AUTH_BASE}/api/accounts/workspace/select"
    headers = json_headers(f"{AUTH_BASE}/workspace", device_id)
    resp, error = request_with_retry(
        session,
        "post",
        url,
        json={"workspace_id": workspace_id},
        headers=headers,
        verify=True,
    )
    if resp is None:
        raise LoginError(f"Workspace select failed: {error or 'no response'}")
    if resp.status_code != 200:
        retry_headers = dict(headers)
        retry_headers["openai-sentinel-token"] = build_sentinel_token(
            session,
            device_id,
            "authorize_continue",
            user_agent=USER_AGENT,
        )[0]
        resp, error = request_with_retry(
            session,
            "post",
            url,
            json={"workspace_id": workspace_id},
            headers=retry_headers,
            verify=True,
        )
        if resp is None or resp.status_code not in (200, 302, 303):
            raise LoginError(
                f"Workspace select failed: HTTP "
                f"{getattr(resp, 'status_code', '?')}"
            )
    return _data_with_response_hints(resp)


def _grant_chatgpt_consent(
    session,
    device_id: str,
) -> dict[str, Any]:
    """Grant a Sign in with ChatGPT consent page when it appears."""
    url = f"{AUTH_BASE}/api/accounts/consent/grant"
    headers = json_headers(f"{AUTH_BASE}/sign-in-with-chatgpt/consent", device_id)
    resp, error = request_with_retry(
        session,
        "post",
        url,
        headers=headers,
        verify=True,
    )
    if resp is None:
        raise LoginError(f"Consent grant failed: {error or 'no response'}")
    if resp.status_code not in (200, 302, 303):
        raise LoginError(
            f"Consent grant failed: HTTP {getattr(resp, 'status_code', '?')}"
        )
    return _data_with_response_hints(resp)


def _complete_login_flow(
    session,
    device_id: str,
    email: str,
    mail_config: dict,
    workspace_id: str,
    response_data: dict[str, Any],
    code_verifier: str,
    password: str | None,
) -> tuple[str, str]:
    """Drive the auth session until it returns a platform callback code."""
    data = response_data
    active_code_verifier = code_verifier
    otp_not_before: datetime | None = None

    for _ in range(14):
        code = _extract_code_from_data(data)
        if code:
            return code, active_code_verifier

        page_type = _page_type_from_data(data)
        logger.info(
            f"[{email}] Team login: continuation page="
            f"{page_type or '(none)'}"
        )
        if not page_type:
            break

        if page_type in {"login_password", "password"}:
            if not password:
                raise PasswordRequiredError(
                    "OTP-only login reached password page"
                )
            logger.info(f"[{email}] Team login: continuing password page")
            data = _handle_password_verification(
                session=session,
                device_id=device_id,
                password=password,
            )
            continue

        if page_type == "email_otp_send":
            otp_not_before = datetime.now(timezone.utc) - timedelta(seconds=5)
            _send_login_otp(session)
            data = {
                "page": {"type": "email_otp_verification"},
            }
            continue

        if page_type in {
            "email_otp_verification",
            "email_otp_verification_registration",
        }:
            if otp_not_before is None:
                otp_not_before = datetime.now(timezone.utc) - timedelta(seconds=5)
                _send_login_otp(session)
            data = _validate_login_otp(
                session=session,
                device_id=device_id,
                email=email,
                mail_config=mail_config,
                not_before=otp_not_before,
            )
            otp_not_before = None
            continue

        if page_type == "workspace":
            data = _select_team_workspace(
                session=session,
                device_id=device_id,
                workspace_id=workspace_id,
                response_data=data,
            )
            continue

        if page_type in {"about_you", "create_account"}:
            data = _complete_about_you(
                session=session,
                device_id=device_id,
                email=email,
            )
            continue

        if page_type == "existing_account":
            code, active_code_verifier, data = _authenticated_authorize(
                session=session,
                device_id=device_id,
                email=email,
            )
            if code:
                return code, active_code_verifier
            continue

        if page_type in {
            "sign_in_with_chatgpt_consent",
            "sign_in_with_chatgpt_codex_consent",
        }:
            data = _grant_chatgpt_consent(session, device_id)
            continue

        raise LoginError(f"Unsupported login continuation page: {page_type}")

    raise LoginError(
        "Authorization code not found after login flow. "
        f"Last page type={_page_type_from_data(data) or '(none)'}"
    )


def _exchange_login_tokens(
    session,
    code_verifier: str,
    code: str,
) -> dict:
    """Exchange the original authorize callback code for OAuth tokens."""
    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "auth0-client": PLATFORM_AUTH0_CLIENT,
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": PLATFORM_BASE,
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": f"{PLATFORM_BASE}/",
        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": USER_AGENT,
    }

    # Exchange code for tokens
    resp = session.post(
        f"{AUTH_BASE}/api/accounts/oauth/token",
        headers=headers,
        json={
            "client_id": PLATFORM_OAUTH_CLIENT_ID,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": PLATFORM_OAUTH_REDIRECT_URI,
        },
        verify=True,
        timeout=60,
    )

    if resp.status_code != 200:
        raise LoginError(
            f"Token exchange failed: HTTP {resp.status_code}, "
            f"{resp.text[:300]}"
        )

    data = _response_json(resp)
    if not data or not data.get("access_token"):
        raise LoginError("Token exchange returned no access_token")

    return data


# ── Simple re-login (without workspace selection) ───────────────────


def re_login_personal(
    email: str,
    password: str,
    mail_config: dict,
    proxy: str = "",
    flaresolverr_url: str = "",
) -> dict:
    """Re-login without workspace selection (gets personal-scope tokens).

    This is useful for refreshing tokens when workspace selection
    is not needed.
    """
    # This is essentially the same flow but without the workspace
    # selection step. For now, reuse re_login_for_team_token
    # without a workspace_id to get whichever scope comes back.
    return re_login_for_team_token(
        email=email,
        password=password,
        mail_config=mail_config,
        proxy=proxy,
        flaresolverr_url=flaresolverr_url,
        workspace_id="",
    )
