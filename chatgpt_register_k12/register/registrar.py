"""ChatGPT account registration engine.

Adapted from chatgpt2api's openai_register.py — stripped of web
dependencies for standalone CLI use.

The PlatformRegistrar class implements the complete 10-step OpenAI
registration flow: create email → authorize → register → OTP →
validate → create account → exchange tokens.
"""

from __future__ import annotations

import json
import logging
import random
import secrets
import string
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from chatgpt_register_k12.register.headers import json_headers, navigate_headers
from chatgpt_register_k12.register.mail_provider import (
    create_mailbox,
    mark_mailbox_result,
    release_mailbox,
    wait_for_code,
)
from chatgpt_register_k12.register.session import (
    create_register_session,
    is_cloudflare_challenge,
    request_with_retry,
)
from chatgpt_register_k12.utils.pkce import generate_pkce
from chatgpt_register_k12.utils.sentinel import build_sentinel_token

# ── OpenAI Auth constants ───────────────────────────────────────────

AUTH_BASE = "https://auth.openai.com"
PLATFORM_BASE = "https://platform.openai.com"
PLATFORM_OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
PLATFORM_OAUTH_REDIRECT_URI = f"{PLATFORM_BASE}/auth/callback"
PLATFORM_OAUTH_AUDIENCE = "https://api.openai.com/v1"
PLATFORM_AUTH0_CLIENT = (
    "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
)

DEFAULT_TIMEOUT = 30

# ── Helpers ─────────────────────────────────────────────────────────


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    first = random.choice(
        ["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]
    )
    last = random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )
    return first, last


def _random_birthdate() -> str:
    return (
        f"{random.randint(1996, 2006):04d}-"
        f"{random.randint(1, 12):02d}-"
        f"{random.randint(1, 28):02d}"
    )


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _response_debug(resp, limit: int = 500) -> str:
    if resp is None:
        return "resp=None"
    data = _response_json(resp)
    parts = [
        f"url={str(getattr(resp, 'url', '') or '')[:200]}",
        f"status={getattr(resp, 'status_code', '?')}",
    ]
    if data:
        parts.append(f"json={json_dumps(data)[:limit]}")
    else:
        try:
            parts.append(f"body={str(resp.text or '')[:limit]}")
        except Exception:
            pass
    return ", ".join(parts)


def json_dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def _extract_oauth_params(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {
        "code": code,
        "state": str((params.get("state") or [""])[0]).strip(),
        "scope": str((params.get("scope") or [""])[0]).strip(),
    }


def _page_type_from_response(resp) -> str:
    data = _response_json(resp)
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    return str(page.get("type") or "")


# ── PlatformRegistrar ───────────────────────────────────────────────


class RegistrationError(RuntimeError):
    """Registration step failed."""


class PlatformRegistrar:
    """Complete ChatGPT platform registration pipeline.

    Usage:
        registrar = PlatformRegistrar(proxy="socks5://...", flaresolverr_url="...")
        result = registrar.register(index=1)
        # result = {email, password, access_token, refresh_token, id_token, ...}
    """

    def __init__(
        self,
        proxy: str = "",
        flaresolverr_url: str = "",
        mail_config: dict | None = None,
    ):
        self.proxy = str(proxy or "").strip()
        self.flaresolverr_url = str(flaresolverr_url or "").strip()
        self.mail_config = mail_config or {}
        self.session = create_register_session(
            proxy=self.proxy,
            flaresolverr_url=self.flaresolverr_url,
        )
        self.device_id = str(uuid.uuid4())
        self.code_verifier = ""
        self.platform_auth_code = ""
        self.passwordless_flow = False

    def close(self) -> None:
        self.session.close()

    # ── Cloudflare retry wrapper ─────────────────────────────────

    def _cf_retry(self, url: str, make_request, label: str = "") -> Any:
        """Standard Cloudflare retry pattern.

        Tries the request; if Cloudflare-challenged, refreshes clearance
        and retries once. Raises RegistrationError on persistent block.
        """
        resp, error = make_request()

        if not is_cloudflare_challenge(resp):
            if resp is None:
                raise RegistrationError(
                    f"[{label}] request failed: {error or 'no response'}"
                )
            return resp

        # Refresh clearance and retry
        if not self.flaresolverr_url:
            status = getattr(resp, "status_code", "?")
            raise RegistrationError(
                f"[{label}] Cloudflare block (no FlareSolverr). "
                f"HTTP {status}. Configure proxy.flaresolverr_url."
            )

        from chatgpt_register_k12.register.session import (
            refresh_clearance_and_retry,
        )
        from urllib.parse import urlparse

        target_host = urlparse(url).hostname or "auth.openai.com"
        if not refresh_clearance_and_retry(
            self.session,
            f"https://{target_host}/",
            self.proxy,
            self.flaresolverr_url,
        ):
            raise RegistrationError(
                f"[{label}] Clearance refresh failed for {target_host}"
            )

        resp2, error2 = make_request()
        if is_cloudflare_challenge(resp2) or resp2 is None:
            raise RegistrationError(
                f"[{label}] Still blocked after clearance refresh. "
                f"Try changing IP/proxy."
            )
        return resp2

    # ── Step 4: Platform authorize ───────────────────────────────

    def _platform_authorize(self, email: str, index: int) -> None:
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")

        self.code_verifier, code_challenge = generate_pkce()
        params = {
            "issuer": AUTH_BASE,
            "client_id": PLATFORM_OAUTH_CLIENT_ID,
            "audience": PLATFORM_OAUTH_AUDIENCE,
            "redirect_uri": PLATFORM_OAUTH_REDIRECT_URI,
            "device_id": self.device_id,
            "screen_hint": "signup",
            "max_age": "0",
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": PLATFORM_AUTH0_CLIENT,
        }
        target_url = f"{AUTH_BASE}/api/accounts/authorize?{urlencode(params)}"
        headers = navigate_headers(f"{PLATFORM_BASE}/")

        def _do():
            return request_with_retry(
                self.session,
                "get",
                target_url,
                headers=headers,
                allow_redirects=True,
                verify=True,
            )

        resp = self._cf_retry(target_url, _do, "authorize")
        if resp.status_code != 200:
            err = _response_json(resp).get("error", {})
            detail = (
                f": {err.get('code', '')} - {err.get('message', '')}".strip(" -")
                if err
                else ""
            )
            raise RegistrationError(
                f"platform_authorize HTTP {resp.status_code}{detail}, "
                f"{_response_debug(resp)}"
            )

        final_url = str(getattr(resp, "url", "") or "")
        if "/create-account" not in final_url:
            raise RegistrationError(
                f"platform_authorize did not enter signup flow: {final_url[:200]}"
            )

    # ── Step 4b: Continue signup with email ──────────────────────

    def _continue_signup_with_email(self, email: str, index: int) -> str:
        """Submit the selected email and return the next auth page type."""
        url = f"{AUTH_BASE}/api/accounts/authorize/continue"
        headers = json_headers(f"{AUTH_BASE}/create-account", self.device_id)
        headers["openai-sentinel-token"] = build_sentinel_token(
            self.session, self.device_id, "authorize_continue"
        )[0]

        def _do():
            return request_with_retry(
                self.session,
                "post",
                url,
                json={
                    "username": {
                        "kind": "email",
                        "value": email,
                    },
                    "screen_hint": "signup",
                },
                headers=headers,
                verify=True,
            )

        resp = self._cf_retry(url, _do, "authorize_continue")
        data = _response_json(resp)
        page_type = _page_type_from_response(resp)
        continue_url = str(data.get("continue_url") or "")
        if resp.status_code == 200 and page_type == "create_account_password":
            self.passwordless_flow = False
            return page_type
        if resp.status_code == 200 and page_type in {
            "email_otp_verification",
            "email_otp_verification_registration",
        }:
            self.passwordless_flow = True
            return page_type

        if resp.status_code != 200 or not page_type:
            raise RegistrationError(
                f"authorize_continue HTTP {resp.status_code}, "
                f"page={page_type or '?'}, continue_url={continue_url[:200]}, "
                f"{_response_debug(resp)}"
            )
        raise RegistrationError(
            f"authorize_continue reached unsupported page={page_type}, "
            f"continue_url={continue_url[:200]}"
        )

    # ── Step 5: Register user ────────────────────────────────────

    def _register_user(self, email: str, password: str, index: int) -> None:
        url = f"{AUTH_BASE}/api/accounts/user/register"
        headers = json_headers(f"{AUTH_BASE}/create-account/password", self.device_id)
        headers["openai-sentinel-token"] = build_sentinel_token(
            self.session, self.device_id, "username_password_create"
        )[0]

        def _do():
            return request_with_retry(
                self.session,
                "post",
                url,
                json={"username": email, "password": password},
                headers=headers,
                verify=True,
            )

        resp = self._cf_retry(url, _do, "register_user")
        if resp.status_code != 200:
            data = _response_json(resp)
            if data.get("message") == "Failed to create account. Please try again.":
                raise RegistrationError(
                    "Email domain likely banned for abuse. Try a different domain."
                )
            raise RegistrationError(
                f"user_register HTTP {resp.status_code}, "
                f"{_response_debug(resp)}"
            )

    # ── Step 6: Send OTP ─────────────────────────────────────────

    def _send_otp(self, index: int) -> None:
        url = f"{AUTH_BASE}/api/accounts/email-otp/send"
        referer = (
            f"{AUTH_BASE}/email-verification"
            if self.passwordless_flow
            else f"{AUTH_BASE}/create-account/password"
        )
        headers = navigate_headers(referer)

        def _do():
            return request_with_retry(
                self.session,
                "get",
                url,
                headers=headers,
                allow_redirects=True,
                verify=True,
            )

        resp = self._cf_retry(url, _do, "send_otp")
        if resp.status_code not in (200, 302):
            raise RegistrationError(
                f"send_otp HTTP {resp.status_code}, {_response_debug(resp)}"
            )

    # ── Step 8: Validate OTP ─────────────────────────────────────

    def _record_oauth_code_from_response(self, resp) -> str:
        data = _response_json(resp)
        continue_url = str(data.get("continue_url") or "").strip()
        callback_params = _extract_oauth_params(continue_url)
        code = str((callback_params or {}).get("code") or "").strip()
        if code:
            self.platform_auth_code = code
        return _page_type_from_response(resp)

    def _finalize_authorize_for_code(self, email: str, index: int) -> bool:
        """Try to convert an authenticated session into an OAuth code."""
        self.code_verifier, code_challenge = generate_pkce()
        params = {
            "issuer": AUTH_BASE,
            "client_id": PLATFORM_OAUTH_CLIENT_ID,
            "audience": PLATFORM_OAUTH_AUDIENCE,
            "redirect_uri": PLATFORM_OAUTH_REDIRECT_URI,
            "device_id": self.device_id,
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
        headers = navigate_headers(f"{PLATFORM_BASE}/")

        def _do():
            return request_with_retry(
                self.session,
                "get",
                url,
                headers=headers,
                allow_redirects=True,
                verify=True,
            )

        resp = self._cf_retry(url, _do, "final_authorize")
        final_url = str(getattr(resp, "url", "") or "")
        callback_params = _extract_oauth_params(final_url)
        code = str((callback_params or {}).get("code") or "").strip()
        if code:
            self.platform_auth_code = code
            return True

        data = _response_json(resp)
        continue_url = str(data.get("continue_url") or "").strip()
        callback_params = _extract_oauth_params(continue_url)
        code = str((callback_params or {}).get("code") or "").strip()
        if code:
            self.platform_auth_code = code
            return True
        return False

    def _validate_otp(self, code: str, index: int) -> str:
        headers = json_headers(f"{AUTH_BASE}/email-verification", self.device_id)

        def _do_simple():
            return request_with_retry(
                self.session,
                "post",
                f"{AUTH_BASE}/api/accounts/email-otp/validate",
                json={"code": code},
                headers=headers,
                verify=True,
            )

        resp, error = _do_simple()
        if resp is not None and resp.status_code == 200:
            return self._record_oauth_code_from_response(resp)

        # Retry with sentinel token
        headers["openai-sentinel-token"] = build_sentinel_token(
            self.session, self.device_id, "authorize_continue"
        )[0]

        resp2, error2 = request_with_retry(
            self.session,
            "post",
            f"{AUTH_BASE}/api/accounts/email-otp/validate",
            json={"code": code},
            headers=headers,
            verify=True,
        )
        if resp2 is None or resp2.status_code != 200:
            body = ""
            try:
                body = (resp2.text or "")[:300] if resp2 else ""
            except Exception:
                pass
            raise RegistrationError(
                f"validate_otp HTTP "
                f"{getattr(resp2, 'status_code', '?')}, body={body}"
            )
        return self._record_oauth_code_from_response(resp2)

    # ── Step 9: Create account ───────────────────────────────────

    def _create_account(self, name: str, birthdate: str, index: int) -> None:
        url = f"{AUTH_BASE}/api/accounts/create_account"
        headers = json_headers(f"{AUTH_BASE}/about-you", self.device_id)
        headers["openai-sentinel-token"] = build_sentinel_token(
            self.session, self.device_id, "oauth_create_account"
        )[0]

        def _do():
            return request_with_retry(
                self.session,
                "post",
                url,
                json={"name": name, "birthdate": birthdate},
                headers=headers,
                verify=True,
                allow_redirects=False,  # Don't follow — we need to extract code from redirect
            )

        resp = self._cf_retry(url, _do, "create_account")
        if resp.status_code not in (200, 302, 303):
            data = _response_json(resp)
            if data.get("message") == "Failed to create account. Please try again.":
                raise RegistrationError(
                    "Account creation failed: email domain likely banned."
                )
            raise RegistrationError(
                f"create_account HTTP {resp.status_code}, {_response_debug(resp)}"
            )

        # Extract auth code from redirect URL or JSON body
        data = _response_json(resp)
        continue_url = str(data.get("continue_url") or "").strip()

        # If no continue_url in body, check the Location header (302 redirect)
        if not continue_url and resp.status_code in (302, 303):
            location = resp.headers.get("Location", "")
            if location:
                continue_url = location

        callback_params = _extract_oauth_params(continue_url)
        self.platform_auth_code = (
            str((callback_params or {}).get("code") or "").strip()
        )

        if not self.platform_auth_code:
            raise RegistrationError(
                f"No auth code in create_account response. "
                f"status={resp.status_code}, "
                f"continue_url={continue_url[:200] if continue_url else '(empty)'}"
            )

    # ── Step 10: Exchange tokens ─────────────────────────────────

    def _exchange_tokens(self, index: int) -> dict:
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
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36",
        }
        resp = self.session.post(
            f"{AUTH_BASE}/api/accounts/oauth/token",
            headers=headers,
            json={
                "client_id": PLATFORM_OAUTH_CLIENT_ID,
                "code_verifier": self.code_verifier,
                "grant_type": "authorization_code",
                "code": self.platform_auth_code,
                "redirect_uri": PLATFORM_OAUTH_REDIRECT_URI,
            },
            verify=True,
            timeout=60,
        )
        if resp.status_code != 200:
            raise RegistrationError(
                f"token exchange HTTP {resp.status_code}, "
                f"{_response_debug(resp)}"
            )
        tokens = _response_json(resp)
        if not tokens or not tokens.get("access_token"):
            raise RegistrationError("token exchange returned no access_token")
        return tokens

    # ── Main registration flow ───────────────────────────────────

    def register(self, index: int = 0) -> dict:
        """Run the complete registration flow.

        Returns:
            {email, password, access_token, refresh_token, id_token,
             source_type, created_at}
        """
        # Step 1: Create mailbox
        mailbox = create_mailbox(self.mail_config, username=None)
        email = str(mailbox.get("address") or "").strip()
        if not email:
            release_mailbox(mailbox)
            raise RegistrationError("Mail provider did not return an address")

        try:
            password = _random_password()
            first_name, last_name = _random_name()

            # Step 4: Platform authorize (PKCE + screen_hint=signup)
            self._platform_authorize(email, index)

            # Step 4b: Submit email and enter password or passwordless flow
            next_page = self._continue_signup_with_email(email, index)

            # Step 5: Register password-based users. Some Outlook accounts
            # are sent directly to email OTP/passwordless auth by OpenAI.
            if next_page == "create_account_password":
                self._register_user(email, password, index)

            # Step 6: Send OTP
            self._send_otp(index)

            # Step 7: Wait for verification code
            mailbox["_code_subject_hint"] = "verification"
            code = wait_for_code(self.mail_config, mailbox)
            if not code:
                raise RegistrationError("Timed out waiting for verification code")
            logging.getLogger(__name__).info(
                f"[Task {index}] Got verification code"
            )

            # Step 8: Validate OTP
            next_page = self._validate_otp(code, index)

            # Step 9: Create account when OTP validation continues to about-you.
            if not self.platform_auth_code:
                if next_page not in {"about_you", "create_account"}:
                    if self.passwordless_flow and self._finalize_authorize_for_code(
                        email, index
                    ):
                        next_page = "oauth_callback"
                    else:
                        raise RegistrationError(
                            f"OTP validation did not return an OAuth code "
                            f"or account creation page (page={next_page or '?'})"
                        )

            if not self.platform_auth_code:
                try:
                    self._create_account(
                        f"{first_name} {last_name}", _random_birthdate(), index
                    )
                except RegistrationError as error:
                    if (
                        self.passwordless_flow
                        and "user_already_exists" in str(error)
                        and self._finalize_authorize_for_code(email, index)
                    ):
                        pass
                    else:
                        raise

            if not self.platform_auth_code:
                if next_page == "oauth_callback":
                    raise RegistrationError("OAuth callback did not contain code")
                else:
                    raise RegistrationError(
                        "Account creation completed without an OAuth code"
                    )

            # Step 10: Exchange tokens
            tokens = self._exchange_tokens(index)

        except Exception as error:
            mark_mailbox_result(mailbox, success=False, error=error)
            raise

        mark_mailbox_result(mailbox, success=True)

        return {
            "email": email,
            "password": password,
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "source_type": "web",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


# ── Worker function (for thread pool) ───────────────────────────────


def register_worker(
    index: int,
    proxy: str = "",
    flaresolverr_url: str = "",
    mail_config: dict | None = None,
) -> dict:
    """Single registration worker. Returns {ok: bool, index, result|error}."""
    start = time.time()
    registrar = PlatformRegistrar(
        proxy=proxy,
        flaresolverr_url=flaresolverr_url,
        mail_config=mail_config,
    )
    try:
        result = registrar.register(index)
        cost = time.time() - start
        return {"ok": True, "index": index, "result": result, "cost_seconds": round(cost, 1)}
    except Exception as e:
        cost = time.time() - start
        return {"ok": False, "index": index, "error": str(e), "cost_seconds": round(cost, 1)}
    finally:
        registrar.close()
