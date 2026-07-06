"""Proxy helpers for curl_cffi sessions.

Simplified from chatgpt2api's proxy_service.py — standalone version
that provides:
  - Proxy URL normalization (socks:// → socks5h://)
  - curl_cffi session kwarg building
  - Cloudflare clearance via FlareSolverr (optional)
  - ClearanceBundle dataclass for cookie/UA caching
"""

from __future__ import annotations

import json as _json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from curl_cffi import requests


# ── Proxy URL normalization ──────────────────────────────────────────

def normalize_proxy_url(url: str | None) -> str:
    """Normalize a proxy URL for curl_cffi.

    - Strips whitespace
    - Converts socks:// to socks5h:// (remote DNS resolution)
    - Returns empty string if invalid
    """
    url = (url or "").strip()
    if not url:
        return ""
    if re.match(r"^socks://", url, re.IGNORECASE):
        url = "socks5h" + url[5:]
    elif re.match(r"^socks4a?://", url, re.IGNORECASE):
        url = re.sub(r"^socks4a?://", "socks4://", url, count=1, flags=re.IGNORECASE)
    return url


# ── ClearanceBundle ──────────────────────────────────────────────────

@dataclass
class ClearanceBundle:
    """Holds Cloudflare clearance cookies and User-Agent for a target host."""

    target_host: str = ""
    proxy_url: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    user_agent: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 3600.0  # 1 hour from creation by default

    def is_valid_for(self, target_host: str = "", proxy_url: str = "") -> bool:
        """Check if this clearance is still fresh for the given target."""
        if time.time() > self.created_at + self.expires_at:
            return False
        if target_host and self.target_host != target_host:
            return False
        if proxy_url and normalize_proxy_url(proxy_url) != normalize_proxy_url(self.proxy_url):
            return False
        return bool(self.cookies)


# ── FlareSolverr Clearance ───────────────────────────────────────────

class FlareSolverrClearanceProvider:
    """Fetch Cloudflare clearance cookies via FlareSolverr."""

    def __init__(self, flaresolverr_url: str, timeout_ms: int = 60000):
        self._flaresolverr_url = flaresolverr_url.rstrip("/")
        self._timeout_ms = timeout_ms

    def fetch_clearance(
        self, target_url: str, proxy_url: str = ""
    ) -> ClearanceBundle | None:
        """Request clearance from FlareSolverr for target_url.

        Returns a ClearanceBundle on success, None on failure.
        """
        fs_url = f"{self._flaresolverr_url}/v1"
        payload: dict[str, Any] = {
            "cmd": "request.get",
            "url": target_url,
            "maxTimeout": self._timeout_ms,
        }
        if proxy_url:
            payload["proxy"] = {"url": proxy_url}

        try:
            response = requests.post(
                fs_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self._timeout_ms / 1000 + 15,
            )
            data = response.json()
        except Exception:
            return None

        solution = data.get("solution", {}) if isinstance(data, dict) else {}
        cookies_list = solution.get("cookies", [])
        user_agent = solution.get("userAgent", "")

        if not cookies_list:
            return None

        # Extract target host from URL for cookie domain scoping
        from urllib.parse import urlparse
        try:
            host = urlparse(target_url).hostname or ""
        except Exception:
            host = ""

        cookies: dict[str, str] = {}
        for cookie in cookies_list:
            name = cookie.get("name", "")
            value = cookie.get("value", "")
            if name and value:
                cookies[name] = value

        return ClearanceBundle(
            target_host=host,
            proxy_url=proxy_url,
            cookies=cookies,
            user_agent=user_agent,
            created_at=time.time(),
            expires_at=1800.0,  # 30 minutes
        )


# ── Session building ─────────────────────────────────────────────────

# Per-proxy clearance cache (simple dict, no persistence)
_clearance_cache: dict[str, ClearanceBundle] = {}
_clearance_lock = threading.Lock()


def build_session_kwargs(
    proxy: str = "",
    impersonate: str = "chrome",
    verify: bool = True,
) -> dict[str, Any]:
    """Build kwargs for constructing a curl_cffi requests.Session.

    Args:
        proxy: SOCKS5/HTTP proxy URL
        impersonate: TLS fingerprint target (default: chrome)
        verify: Whether to verify SSL certs
    """
    kwargs: dict[str, Any] = {
        "impersonate": impersonate,
        "verify": verify,
    }
    proxy_url = normalize_proxy_url(proxy)
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return kwargs


def create_session(
    proxy: str = "",
    impersonate: str = "chrome",
    verify: bool = True,
) -> requests.Session:
    """Create a curl_cffi requests.Session with proxy and TLS impersonation."""
    kwargs = build_session_kwargs(proxy=proxy, impersonate=impersonate, verify=verify)
    return requests.Session(**kwargs)


def apply_clearance_to_session(
    session: requests.Session,
    bundle: ClearanceBundle | None,
) -> None:
    """Inject clearance cookies and User-Agent into a curl_cffi session."""
    if bundle is None:
        return
    if bundle.user_agent:
        session.headers["User-Agent"] = bundle.user_agent
        session.headers["user-agent"] = bundle.user_agent
    for name, value in bundle.cookies.items():
        try:
            session.cookies.set(name, value, domain=f".{bundle.target_host or 'openai.com'}")
        except Exception:
            pass


def get_cached_clearance(target_host: str, proxy: str = "") -> ClearanceBundle | None:
    """Get a cached clearance bundle for the target host + proxy combo."""
    key = f"{normalize_proxy_url(proxy)}::{target_host}"
    with _clearance_lock:
        bundle = _clearance_cache.get(key)
        if bundle and bundle.is_valid_for(target_host, proxy):
            return bundle
    return None


def set_cached_clearance(bundle: ClearanceBundle) -> None:
    """Cache a clearance bundle."""
    key = f"{normalize_proxy_url(bundle.proxy_url)}::{bundle.target_host}"
    with _clearance_lock:
        _clearance_cache[key] = bundle
