"""Multi-target JSON exporters for ChatGPT account records."""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chatgpt_register_sub2api.export.sub2api import (
    build_sub2api_bundle,
    build_credentials,
)
from chatgpt_register_sub2api.utils.jwt import extract_account_info


DEFAULT_EXPORT_FORMAT = "sub2api"

EXPORT_TARGETS: dict[str, dict[str, str]] = {
    "sub2api": {"label": "Sub2API", "filename": "sub2api_bundle.json"},
    "auth": {"label": "auth.json", "filename": "auth.json"},
    "raw-session": {"label": "Session JSON", "filename": "session.json"},
    "cpa": {"label": "CPA", "filename": "cpa.json"},
    "cockpit": {"label": "Cockpit", "filename": "cockpit.json"},
    "9router": {"label": "9router", "filename": "9router.json"},
    "axonhub": {"label": "AxonHub", "filename": "axonhub-auth.json"},
}

AXONHUB_PLACEHOLDER_REFRESH_TOKEN = "__missing_refresh_token__"


def supported_export_formats() -> list[str]:
    return list(EXPORT_TARGETS)


def normalize_export_format(value: Any) -> str:
    selected = str(value or DEFAULT_EXPORT_FORMAT).strip().lower()
    aliases = {
        "session": "raw-session",
        "raw_session": "raw-session",
        "nine-router": "9router",
        "nine_router": "9router",
    }
    selected = aliases.get(selected, selected)
    if selected not in EXPORT_TARGETS:
        raise ValueError(
            "Unsupported export format: "
            f"{value}. Supported formats: {', '.join(supported_export_formats())}"
        )
    return selected


def export_format_from_config(config: dict[str, Any]) -> str:
    export_cfg = config.get("export", {})
    if not isinstance(export_cfg, dict):
        export_cfg = {}
    return normalize_export_format(export_cfg.get("format") or DEFAULT_EXPORT_FORMAT)


def default_output_filename(export_format: str) -> str:
    return EXPORT_TARGETS[normalize_export_format(export_format)]["filename"]


def output_filename_from_config(config: dict[str, Any]) -> str:
    export_format = export_format_from_config(config)
    export_cfg = config.get("export", {})
    if not isinstance(export_cfg, dict):
        export_cfg = {}
    configured = str(export_cfg.get("output_file") or "").strip()
    if configured:
        return configured
    if export_format == "sub2api":
        sub2api_cfg = config.get("sub2api", {})
        if isinstance(sub2api_cfg, dict):
            legacy = str(sub2api_cfg.get("output_file") or "").strip()
            if legacy:
                return legacy
    return default_output_filename(export_format)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _iso_from_epoch(seconds: Any) -> str:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        value = int(time.time()) + 863999
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _epoch_from_iso(value: str) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _expires_in(expires_at: str, now: datetime) -> int | None:
    expires = _epoch_from_iso(expires_at)
    if not expires:
        return None
    return max(0, expires - int(now.timestamp()))


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _strip_unavailable(value: Any) -> Any:
    if isinstance(value, list):
        items = [_strip_unavailable(item) for item in value]
        return [item for item in items if item is not None]
    if isinstance(value, dict):
        result = {
            key: _strip_unavailable(item)
            for key, item in value.items()
        }
        result = {
            key: item
            for key, item in result.items()
            if item is not None
        }
        return result or None
    if value is None or value == "":
        return None
    return value


def _email_key(email: str) -> str:
    chars = []
    last_underscore = False
    for char in email.strip().lower():
        if char.isalnum():
            chars.append(char)
            last_underscore = False
        elif not last_underscore:
            chars.append("_")
            last_underscore = True
    return "".join(chars).strip("_")


def _base64url_json(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _build_synthetic_id_token(context: dict[str, Any]) -> str:
    account_id = str(context.get("account_id") or "").strip()
    if not account_id:
        return ""
    now_seconds = int(context["now"].timestamp())
    expires_at = _epoch_from_iso(str(context.get("expires_at") or ""))
    payload: dict[str, Any] = {
        "iat": now_seconds,
        "exp": expires_at or now_seconds + 90 * 24 * 60 * 60,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
        },
    }
    if context.get("plan_type"):
        payload["https://api.openai.com/auth"]["chatgpt_plan_type"] = context["plan_type"]
    if context.get("user_id"):
        payload["https://api.openai.com/auth"]["chatgpt_user_id"] = context["user_id"]
        payload["https://api.openai.com/auth"]["user_id"] = context["user_id"]
    if context.get("email"):
        payload["email"] = context["email"]
    header = {"alg": "none", "typ": "JWT", "cpa_synthetic": True}
    return f"{_base64url_json(header)}.{_base64url_json(payload)}.synthetic"


def build_export_context(account: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    access_token = str(account.get("access_token") or "").strip()
    info = extract_account_info(access_token) if access_token else {}
    credentials = build_credentials(account)
    expires_at = _iso_from_epoch(credentials.get("expires_at"))

    email = _first_non_empty(account.get("email"), credentials.get("email"), info.get("email"))
    account_id = _first_non_empty(
        account.get("chatgpt_account_id"),
        credentials.get("chatgpt_account_id"),
        info.get("chatgpt_account_id"),
    )
    user_id = _first_non_empty(
        account.get("chatgpt_user_id"),
        credentials.get("chatgpt_user_id"),
        info.get("chatgpt_user_id"),
    )
    plan_type = _first_non_empty(
        account.get("plan_type"),
        credentials.get("plan_type"),
        info.get("plan_type"),
    )
    id_token = _first_non_empty(account.get("id_token"), credentials.get("id_token"))
    refresh_token = _first_non_empty(account.get("refresh_token"), credentials.get("refresh_token"))
    session_token = _first_non_empty(account.get("session_token"), credentials.get("session_token"))
    context = {
        "account": account,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "session_token": session_token,
        "account_id": account_id,
        "email": email,
        "user_id": user_id,
        "plan_type": plan_type,
        "display_name": _first_non_empty(account.get("name"), email, account_id, "ChatGPT Account"),
        "expires_at": expires_at,
        "expires_at_epoch": credentials.get("expires_at"),
        "exported_at": now.isoformat(),
        "last_refresh": _first_non_empty(account.get("last_refresh"), account.get("refreshed_at"), now.isoformat()),
        "source": _first_non_empty(account.get("source_type"), account.get("source"), "registration"),
        "now": now,
    }
    context["codex_id_token"] = _first_non_empty(
        id_token,
        _build_synthetic_id_token(context),
        access_token,
    )
    context["codex_id_token_synthetic"] = bool(not id_token and account_id)
    return context


def build_auth_entry(account: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    context = build_export_context(account, now)
    session_or_refresh_token = _first_non_empty(
        context["session_token"],
        context["refresh_token"],
    )
    return {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "last_refresh": context["last_refresh"],
        "tokens": {
            "access_token": context["access_token"],
            "account_id": context["account_id"],
            "id_token": context["access_token"],
            "refresh_token": session_or_refresh_token,
        },
    }


def build_raw_session_entry(account: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    context = build_export_context(account, now)
    return _strip_unavailable(
        {
            "accessToken": context["access_token"],
            "refreshToken": context["refresh_token"],
            "idToken": context["id_token"],
            "sessionToken": context["session_token"],
            "expires": context["expires_at"],
            "account": {
                "id": context["account_id"],
                "planType": context["plan_type"],
            },
            "user": {
                "email": context["email"],
                "id": context["user_id"],
            },
            "source": context["source"],
            "exportedAt": context["exported_at"],
        }
    ) or {}


def build_cpa_entry(account: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    context = build_export_context(account, now)
    return _strip_unavailable(
        {
            "type": "codex",
            "account_id": context["account_id"],
            "chatgpt_account_id": context["account_id"],
            "email": context["email"],
            "name": context["display_name"],
            "plan_type": context["plan_type"],
            "chatgpt_plan_type": context["plan_type"],
            "id_token": context["codex_id_token"],
            "id_token_synthetic": context["codex_id_token_synthetic"] or None,
            "access_token": context["access_token"],
            "refresh_token": context["refresh_token"],
            "session_token": context["session_token"],
            "last_refresh": context["exported_at"],
            "expired": context["expires_at"],
        }
    ) or {}


def build_cockpit_entry(account: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    context = build_export_context(account, now)
    return _strip_unavailable(
        {
            "type": "codex",
            "id_token": context["codex_id_token"],
            "access_token": context["access_token"],
            "refresh_token": context["refresh_token"],
            "account_id": context["account_id"],
            "last_refresh": context["exported_at"],
            "email": context["email"],
            "expired": context["expires_at"],
        }
    ) or {}


def build_9router_entry(account: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    context = build_export_context(account, now)
    return _strip_unavailable(
        {
            "accessToken": context["access_token"],
            "refreshToken": context["refresh_token"],
            "expiresAt": context["expires_at"],
            "testStatus": "active",
            "expiresIn": _expires_in(context["expires_at"], context["now"]),
            "providerSpecificData": {
                "chatgptAccountId": context["account_id"],
                "chatgptPlanType": context["plan_type"],
            },
            "id": context["account_id"],
            "provider": "codex",
            "authType": "oauth",
            "name": context["display_name"],
            "email": context["email"],
            "priority": 9,
            "isActive": True,
            "createdAt": context["exported_at"],
            "updatedAt": context["exported_at"],
        }
    ) or {}


def build_axonhub_entry(account: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    context = build_export_context(account, now)
    refresh_token = context["refresh_token"] or AXONHUB_PLACEHOLDER_REFRESH_TOKEN
    last_refresh = context["exported_at"]
    expires = _epoch_from_iso(context["expires_at"])
    if expires:
        last_refresh = datetime.fromtimestamp(expires - 3600, timezone.utc).isoformat()
    return _strip_unavailable(
        {
            "auth_mode": "chatgpt",
            "last_refresh": last_refresh,
            "tokens": {
                "access_token": context["access_token"],
                "refresh_token": refresh_token,
                "id_token": context["codex_id_token"],
            },
            "axonhub_refresh_token_placeholder": None if context["refresh_token"] else True,
            "axonhub_note": None
            if context["refresh_token"]
            else "refresh_token is a placeholder; access_token works only until it expires.",
        }
    ) or {}


def build_export_payload(
    accounts: list[dict[str, Any]],
    export_format: str = DEFAULT_EXPORT_FORMAT,
) -> Any:
    selected = normalize_export_format(export_format)
    now = _now()
    if selected == "sub2api":
        return build_sub2api_bundle(accounts)

    builders = {
        "auth": build_auth_entry,
        "raw-session": build_raw_session_entry,
        "cpa": build_cpa_entry,
        "cockpit": build_cockpit_entry,
        "9router": build_9router_entry,
        "axonhub": build_axonhub_entry,
    }
    entries = [builders[selected](account, now) for account in accounts]
    if selected == "auth" and len(entries) == 1:
        return entries[0]
    return entries


def count_exported_payload(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        accounts = payload.get("accounts")
        if isinstance(accounts, list):
            return len(accounts)
        return 1 if payload else 0
    return 0


def count_exported_json(json_str: str) -> int:
    try:
        payload = json.loads(json_str)
    except Exception:
        return 0
    return count_exported_payload(payload)


def export_accounts_json(
    accounts: list[dict[str, Any]],
    output_path: str | Path | None = None,
    export_format: str = DEFAULT_EXPORT_FORMAT,
) -> tuple[str, str]:
    selected = normalize_export_format(export_format)
    payload = build_export_payload(accounts, selected)
    json_str = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    if output_path is None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_path = Path(f"{selected}-{timestamp}.json")

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_str, encoding="utf-8")
    return json_str, str(path.resolve())
