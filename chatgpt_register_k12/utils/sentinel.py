"""OpenAI Sentinel Token (PoW) generation and request.

Used by registration and login flows that need sentinel tokens
(username_password_create, authorize_continue, oauth_create_account, etc.).

Adapted verbatim from chatgpt2api — the PoW algorithm must match
exactly what OpenAI's sentinel service expects.
"""

from __future__ import annotations

import base64
import json
import random
import time
import uuid

from curl_cffi.requests import Session


class SentinelTokenGenerator:
    """Sentinel Token 生成器（PoW - Proof of Work）."""

    MAX_ATTEMPTS = 500_000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, ua: str):
        self.device_id = device_id
        self.user_agent = ua
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        perf_now = random.uniform(1000, 50000)
        return [
            "1920x1080",
            time.strftime(
                "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)",
                time.gmtime(),
            ),
            4294705152,
            random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            "en-US",
            random.random(),
            random.choice(
                [
                    "vendorSub-undefined",
                    "plugins-undefined",
                    "mimeTypes-undefined",
                    "hardwareConcurrency-undefined",
                ]
            ),
            random.choice(
                [
                    "location",
                    "implementation",
                    "URL",
                    "documentURI",
                    "compatMode",
                ]
            ),
            random.choice(
                [
                    "Object",
                    "Function",
                    "Array",
                    "Number",
                    "parseFloat",
                    "undefined",
                ]
            ),
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time.time() * 1000 - perf_now,
        ]

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(
            json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).decode("ascii")

    def generate_requirements_token(self) -> str:
        data = self._get_config()
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(data)

    def generate_token(self, seed: str, difficulty: str) -> str:
        start = time.time()
        data = self._get_config()
        difficulty = str(difficulty or "0")
        for i in range(self.MAX_ATTEMPTS):
            data[3] = i
            data[9] = round((time.time() - start) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


DEFAULT_SENTINEL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
DEFAULT_SENTINEL_SEC_CH_UA = (
    '"Chromium";v="145", "Google Chrome";v="145", "Not/A)Brand";v="99"'
)


def build_sentinel_token(
    session: Session,
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
) -> tuple[str, str]:
    """Request a sentinel token from OpenAI's PoW service.

    Args:
        session: curl_cffi Session instance
        device_id: Device UUID
        flow: Flow identifier (e.g. "username_password_create",
              "authorize_continue", "oauth_create_account")
        user_agent: Optional User-Agent override
        sec_ch_ua: Optional sec-ch-ua override

    Returns:
        (openai-sentinel-token header value, oai-sc cookie value) tuple
    """
    ua = user_agent or DEFAULT_SENTINEL_USER_AGENT
    ch_ua = sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA
    generator = SentinelTokenGenerator(device_id, ua)

    resp = session.post(
        "https://sentinel.openai.com/backend-api/sentinel/req",
        data=json.dumps(
            {
                "p": generator.generate_requirements_token(),
                "id": device_id,
                "flow": flow,
            }
        ),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
            "Origin": "https://sentinel.openai.com",
            "User-Agent": ua,
            "sec-ch-ua": ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        timeout=20,
        verify=True,
    )

    try:
        data = resp.json() if resp.text else {}
    except Exception:
        fallback = json.dumps(
            {
                "p": generator.generate_requirements_token(),
                "t": "",
                "c": "",
                "id": device_id,
                "flow": flow,
            },
            separators=(",", ":"),
        )
        return fallback, ""

    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")

    pow_data = data.get("proofofwork") or {}
    p_value = (
        generator.generate_token(
            str(pow_data.get("seed") or ""),
            str(pow_data.get("difficulty") or "0"),
        )
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )

    sentinel_value = json.dumps(
        {"p": p_value, "t": "", "c": token, "id": device_id, "flow": flow},
        separators=(",", ":"),
    )
    oai_sc_value = "0" + token
    return sentinel_value, oai_sc_value
