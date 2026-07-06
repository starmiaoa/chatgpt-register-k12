"""Redaction helpers for WebUI responses and logs."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


SECRET_KEYS = {
    "access_token",
    "authorization",
    "client_secret",
    "id_token",
    "mailboxes",
    "password",
    "refresh_token",
    "session_token",
    "team_access_token",
    "team_id_token",
    "team_refresh_token",
}


def mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 12:
        return "***"
    return f"{text[:6]}...REDACTED...{text[-4:]}"


def mask_email(value: str) -> str:
    text = str(value or "").strip()
    if "@" not in text:
        return text
    local, domain = text.rsplit("@", 1)
    if not local:
        return f"***@{domain}"
    return f"{local[:1]}***@{domain}"


def mask_workspace_id(value: str) -> str:
    text = str(value or "").strip()
    if len(text) < 16:
        return text
    return f"{text[:8]}...{text[-4:]}"


def redact_proxy_url(value: str) -> str:
    text = str(value or "")
    try:
        parts = urlsplit(text)
    except Exception:
        return redact_text(text)
    if not parts.username and not parts.password:
        return text
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def redact_mailbox_line(line: str) -> str:
    text = str(line or "").strip()
    if "----" not in text:
        return mask_email(text) if "@" in text else text
    email = text.split("----", 1)[0].strip()
    return f"{mask_email(email)}----***----***----***"


def redact_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = re.sub(
        r"Bearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer ***",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})----[^\s]+",
        lambda m: redact_mailbox_line(m.group(0)),
        text,
    )
    text = re.sub(
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
        lambda m: mask_email(m.group(0)),
        text,
    )
    text = re.sub(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        lambda m: mask_workspace_id(m.group(0)),
        text,
    )
    return text


def redact_object(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if key_lower in SECRET_KEYS or any(secret in key_lower for secret in SECRET_KEYS):
                if key_lower == "mailboxes":
                    result[key_text] = [
                        redact_mailbox_line(line)
                        for line in str(item or "").splitlines()
                        if str(line or "").strip()
                    ]
                elif key_lower == "url" and "proxy" in key_lower:
                    result[key_text] = redact_proxy_url(str(item or ""))
                else:
                    result[key_text] = mask_secret(item)
            elif key_lower in {"email", "address", "login_address", "base_address"}:
                result[key_text] = mask_email(str(item or ""))
            elif key_lower in {"workspace_id", "chatgpt_account_id", "account_id"}:
                result[key_text] = mask_workspace_id(str(item or ""))
            elif key_lower == "url" and isinstance(item, str):
                result[key_text] = redact_proxy_url(item)
            else:
                result[key_text] = redact_object(item)
        return result
    if isinstance(value, list):
        return [redact_object(item) for item in value]
    if isinstance(value, tuple):
        return [redact_object(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
