"""Workspace join client — send join requests to K12 parent workspace.

Adapted from the Tampermonkey userscript:
  子号加入K12母号代码.txt

The flow:
  1. Use the account's access_token as Bearer token
  2. POST /backend-api/accounts/{workspace_id}/invites/{route}
  3. Verify that membership became active
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from curl_cffi import requests

from chatgpt_register_k12.utils.proxy import normalize_proxy_url

CHATGPT_BASE = "https://chatgpt.com"

STANDARD_ROUTES = {"accept", "request"}
K12_BROWSER_ROUTES = {"k12_request", "request_browser", "browser_request"}


def _normalise_route(route: str) -> str:
    return (route or "k12_request").strip().lower().replace("-", "_")


def _build_request(access_token: str, workspace_id: str, route: str) -> tuple[str, dict[str, str], str | None]:
    """Build the workspace invite request for the configured route."""
    mode = _normalise_route(route)

    if mode in K12_BROWSER_ROUTES:
        url = f"{CHATGPT_BASE}/backend-api/accounts/{workspace_id}/invites/request"
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "authorization": f"Bearer {access_token}",
            "cache-control": "no-cache",
            "oai-language": "en-US",
            "pragma": "no-cache",
            "referer": f"{CHATGPT_BASE}/k12-verification",
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-model": '""',
            "sec-ch-ua-platform": '"macOS"',
            "sec-ch-ua-platform-version": '"13.5.1"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        return url, headers, None

    if mode not in STANDARD_ROUTES:
        supported = ", ".join(sorted(STANDARD_ROUTES | K12_BROWSER_ROUTES))
        raise ValueError(f"Unsupported workspace route '{route}'. Supported: {supported}")

    device_id = str(uuid.uuid4())
    url = f"{CHATGPT_BASE}/backend-api/accounts/{workspace_id}/invites/{mode}"
    headers = {
        "accept": "*/*",
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
        "oai-device-id": device_id,
        "oai-language": "en-US",
    }
    return url, headers, ""


def join_workspace(
    access_token: str,
    workspace_id: str,
    route: str = "k12_request",
    max_retries: int = 3,
    retry_backoff_ms: int = 5000,
    session: requests.Session | None = None,
    proxy: str = "",
) -> dict:
    """Send a single workspace join request.

    Args:
        access_token: The account's Bearer token
        workspace_id: Parent workspace UUID
        route: "accept", "request", or "k12_request" (browser-like K12 page request)
        max_retries: Max retry attempts on non-auth errors
        retry_backoff_ms: Backoff between retries (multiplied by attempt)
        proxy: SOCKS5/HTTP proxy URL

    Returns:
        {ok: bool, status_code: int, body: str, workspace_id: str}
    """
    try:
        url, headers, request_body = _build_request(access_token, workspace_id, route)
    except ValueError as e:
        return {
            "ok": False,
            "status_code": 0,
            "body": "",
            "workspace_id": workspace_id,
            "error": str(e),
        }

    if session:
        _session = session
        should_close = False
    else:
        kwargs = {"impersonate": "chrome", "verify": True}
        proxy_url = normalize_proxy_url(proxy)
        if proxy_url:
            kwargs["proxy"] = proxy_url
        _session = requests.Session(**kwargs)
        should_close = True

    try:
        last_status = 0
        last_body = ""
        for attempt in range(max_retries):
            try:
                post_kwargs: dict[str, Any] = {
                    "headers": headers,
                    "timeout": 30,
                }
                if request_body is not None:
                    post_kwargs["data"] = request_body

                resp = _session.post(url, **post_kwargs)
                last_body = resp.text[:500] if resp.text else ""
                last_status = resp.status_code

                if last_status in (401, 403):
                    return {
                        "ok": False,
                        "status_code": last_status,
                        "body": last_body,
                        "workspace_id": workspace_id,
                        "error": "Token expired (401/403). Re-login needed.",
                    }

                if resp.ok:
                    return {
                        "ok": True,
                        "status_code": last_status,
                        "body": last_body,
                        "workspace_id": workspace_id,
                    }

                # Non-auth error — retry with backoff
                if attempt < max_retries - 1:
                    time.sleep(retry_backoff_ms * (attempt + 1) / 1000.0)

            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(retry_backoff_ms / 1000.0)
                else:
                    return {
                        "ok": False,
                        "status_code": 0,
                        "body": "",
                        "workspace_id": workspace_id,
                        "error": str(e),
                    }

        return {
            "ok": False,
            "status_code": last_status,
            "body": last_body,
            "workspace_id": workspace_id,
            "error": f"Max retries ({max_retries}) exhausted",
        }
    finally:
        if should_close:
            _session.close()


def join_workspaces(
    access_token: str,
    workspace_ids: list[str],
    route: str = "k12_request",
    max_retries: int = 3,
    retry_backoff_ms: int = 5000,
    interval_ms: int = 1500,
    proxy: str = "",
) -> list[dict]:
    """Join multiple workspaces sequentially.

    Args:
        access_token: The account's Bearer token
        workspace_ids: List of parent workspace UUIDs
        route: "accept", "request", or "k12_request"
        max_retries: Max retries per workspace
        retry_backoff_ms: Backoff between retries
        interval_ms: Delay between different workspace requests
        proxy: SOCKS5/HTTP proxy URL

    Returns:
        List of result dicts, one per workspace_id
    """
    results = []
    session = None
    try:
        kwargs = {"impersonate": "chrome", "verify": True}
        proxy_url = normalize_proxy_url(proxy)
        if proxy_url:
            kwargs["proxy"] = proxy_url
        session = requests.Session(**kwargs)
        for i, ws_id in enumerate(workspace_ids):
            result = join_workspace(
                access_token=access_token,
                workspace_id=ws_id.strip(),
                route=route,
                max_retries=max_retries,
                retry_backoff_ms=retry_backoff_ms,
                session=session,
                proxy=proxy,
            )
            results.append(result)
            if i < len(workspace_ids) - 1:
                time.sleep(interval_ms / 1000.0)
    finally:
        if session:
            session.close()
    return results
