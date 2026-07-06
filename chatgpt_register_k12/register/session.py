"""curl_cffi session factory with Cloudflare clearance support.

Provides a single `create_session()` call that:
  - Applies proxy settings
  - Sets Chrome impersonation
  - Optionally fetches and applies FlareSolverr clearance cookies
  - Handles clearance refresh on Cloudflare challenge detection
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

from curl_cffi import requests

from chatgpt_register_k12.utils.proxy import (
    ClearanceBundle,
    FlareSolverrClearanceProvider,
    apply_clearance_to_session,
    create_session as _create_session,
    get_cached_clearance,
    normalize_proxy_url,
    set_cached_clearance,
)


def create_register_session(
    proxy: str = "",
    flaresolverr_url: str = "",
    impersonate: str = "chrome",
) -> requests.Session:
    """Create a curl_cffi Session with proxy and optional Cloudflare clearance.

    Args:
        proxy: SOCKS5/HTTP proxy URL
        flaresolverr_url: FlareSolverr endpoint (optional)
        impersonate: TLS fingerprint target
    """
    session = _create_session(proxy=proxy, impersonate=impersonate, verify=True)

    # Pre-warm clearance if FlareSolverr is configured
    if flaresolverr_url:
        _prewarm_clearance(session, proxy, flaresolverr_url)

    return session


def _prewarm_clearance(
    session: requests.Session, proxy: str, flaresolverr_url: str
) -> None:
    """Try to get initial Cloudflare clearance for auth.openai.com."""
    target_host = "auth.openai.com"
    target_url = f"https://{target_host}/"

    # Check cache first
    cached = get_cached_clearance(target_host, proxy)
    if cached is not None:
        apply_clearance_to_session(session, cached)
        return

    # Fetch fresh clearance
    provider = FlareSolverrClearanceProvider(flaresolverr_url)
    bundle = provider.fetch_clearance(target_url, proxy)
    if bundle:
        set_cached_clearance(bundle)
        apply_clearance_to_session(session, bundle)


def is_cloudflare_challenge(resp: requests.Response | None) -> bool:
    """Detect if a response is a Cloudflare challenge page."""
    if resp is None:
        return False
    try:
        status_code = int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code not in (403, 503):
        return False
    text = str(getattr(resp, "text", "") or "").lower()
    return (
        "<title>just a moment" in text
        or "<title>attention required! | cloudflare" in text
        or "cf-chl-" in text
        or "__cf_chl_" in text
        or "cf-browser-verification" in text
    )


def refresh_clearance_and_retry(
    session: requests.Session,
    target_url: str,
    proxy: str = "",
    flaresolverr_url: str = "",
) -> bool:
    """Refresh Cloudflare clearance and apply to session.

    Returns True if clearance was successfully refreshed.
    """
    if not flaresolverr_url:
        return False

    provider = FlareSolverrClearanceProvider(flaresolverr_url)
    bundle = provider.fetch_clearance(target_url, normalize_proxy_url(proxy))
    if bundle is None or not bundle.cookies:
        return False

    set_cached_clearance(bundle)
    apply_clearance_to_session(session, bundle)
    return True


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    retry_attempts: int = 3,
    timeout: int = 30,
    **kwargs,
) -> tuple[requests.Response | None, str]:
    """Make an HTTP request with simple retry on network errors.

    Returns (response, error_string). error_string is empty on success.
    """
    last_error = ""
    for _ in range(max(1, retry_attempts)):
        try:
            resp = session.request(method.upper(), url, timeout=timeout, **kwargs)
            return resp, ""
        except Exception as error:
            last_error = str(error)
            time.sleep(1)
    return None, last_error


def cloudflare_retry_pattern(
    session: requests.Session,
    url: str,
    proxy: str,
    flaresolverr_url: str,
    make_request,
    index: int = 0,
) -> requests.Response | None:
    """Standard Cloudflare retry pattern used by all registration steps.

    1. Make the request
    2. If Cloudflare challenge detected, refresh clearance
    3. Retry once with fresh clearance
    4. If still challenged, raise RuntimeError

    Args:
        session: curl_cffi Session
        url: The URL being requested (for clearance targeting)
        proxy: Proxy URL
        flaresolverr_url: FlareSolverr endpoint (empty = skip clearance)
        make_request: Callable that makes the actual HTTP request
        index: Task index for logging

    Returns:
        The successful response, or raises RuntimeError
    """
    # First attempt
    resp, error = make_request()

    if not is_cloudflare_challenge(resp):
        if resp is None:
            raise RuntimeError(error or "request returned None")
        return resp

    # Cloudflare detected — try to refresh clearance
    if not flaresolverr_url:
        raise RuntimeError(
            f"Cloudflare intercepted (no FlareSolverr configured). "
            f"Status: {getattr(resp, 'status_code', '?')}. "
            f"Set proxy.flaresolverr_url in config.yaml."
        )

    target_host = urlparse(url).hostname or "auth.openai.com"
    if not refresh_clearance_and_retry(session, f"https://{target_host}/", proxy, flaresolverr_url):
        raise RuntimeError(
            f"Cloudflare clearance refresh failed for {target_host}. "
            f"Check FlareSolverr is running and reachable."
        )

    # Retry with fresh clearance
    resp2, error2 = make_request()

    if is_cloudflare_challenge(resp2) or resp2 is None:
        raise RuntimeError(
            error2 or f"Still blocked by Cloudflare after clearance refresh. "
            f"Consider changing IP/proxy."
        )

    return resp2
