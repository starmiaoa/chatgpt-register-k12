"""HTTP header templates for OpenAI Auth0 OAuth flow.

These headers mimic Chrome 145 on Windows — matching what the real
ChatGPT web app sends.  Kept in one place so they're easy to update
when OpenAI changes their browser fingerprint requirements.
"""

from __future__ import annotations

import random
import uuid

# ── Browser fingerprint constants ───────────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

SEC_CH_UA = '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"'
SEC_CH_UA_FULL_VERSION_LIST = (
    '"Chromium";v="145.0.0.0", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="145.0.0.0"'
)

# ── Common JSON API headers ────────────────────────────────────────


def _common_headers() -> dict[str, str]:
    return {
        "accept": "application/json",
        "accept-encoding": "gzip, deflate, br",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "connection": "keep-alive",
        "content-type": "application/json",
        "dnt": "1",
        "origin": "https://auth.openai.com",
        "priority": "u=1, i",
        "sec-gpc": "1",
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-arch": '"x86_64"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-full-version-list": SEC_CH_UA_FULL_VERSION_LIST,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-platform-version": '"10.0.0"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": USER_AGENT,
    }


def common_headers() -> dict[str, str]:
    """Standard JSON API request headers (no referer, no oai-device-id)."""
    return _common_headers()


def json_headers(referer: str, device_id: str) -> dict[str, str]:
    """JSON API request headers with referer + oai-device-id + Datadog traces."""
    headers = _common_headers()
    headers["referer"] = referer
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    return headers


def navigate_headers(referer: str = "") -> dict[str, str]:
    """Browser navigation headers (text/html accept, no content-type)."""
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-encoding": "gzip, deflate, br",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "max-age=0",
        "connection": "keep-alive",
        "dnt": "1",
        "sec-gpc": "1",
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-arch": '"x86_64"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-full-version-list": SEC_CH_UA_FULL_VERSION_LIST,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-platform-version": '"10.0.0"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": USER_AGENT,
    }
    if referer:
        headers["referer"] = referer
    return headers


def _make_trace_headers() -> dict[str, str]:
    """Generate Datadog APM trace headers (expected by OpenAI's backend)."""
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }
