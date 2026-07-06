"""Sub2API bundle export — ChatGPT tokens → sub2api JSON format.

Replicates the conversion logic from https://gpt.learnlicen.dpdns.org/

Input: registered account records (from register or login)
Output: sub2api bundle JSON with team-scoped tokens
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chatgpt_register_k12.utils.jwt import extract_account_info


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_expires_at(
    access_token: str,
    fallback_expired: str = "",
) -> int:
    """Extract exp from JWT or compute fallback (now + ~10 days)."""
    if access_token:
        info = extract_account_info(access_token)
        exp = info.get("exp", 0)
        if exp and exp > 0:
            return exp

    # Fallback: parse expired string or use now + 10 days
    if fallback_expired:
        try:
            return int(fallback_expired)
        except (TypeError, ValueError):
            pass

    return int(time.time()) + 863999  # ~10 days


def build_credentials(
    account: dict[str, Any],
) -> dict[str, Any]:
    """Build the credentials dict for a single sub2api account entry.

    Args:
        account: Account record with keys:
            email, password, access_token, refresh_token, id_token,
            chatgpt_account_id, chatgpt_user_id, plan_type, etc.

    Returns:
        Sub2API credentials dict
    """
    access_token = str(account.get("access_token") or "").strip()
    refresh_token = str(account.get("refresh_token") or "").strip()
    id_token = str(account.get("id_token") or "").strip()
    email = str(account.get("email") or "").strip()

    # Extract claims from access token JWT
    info = extract_account_info(access_token) if access_token else {}

    chatgpt_account_id = (
        str(account.get("chatgpt_account_id") or "").strip()
        or info.get("chatgpt_account_id", "")
    )
    chatgpt_user_id = (
        str(account.get("chatgpt_user_id") or "").strip()
        or info.get("chatgpt_user_id", "")
    )
    plan_type = (
        str(account.get("plan_type") or "").strip()
        or info.get("plan_type", "free")
    )
    organization_id = (
        str(account.get("organization_id") or "").strip()
        or info.get("organization_id", "")
    )
    user_email = email or info.get("email", "")

    expires_at = _coerce_expires_at(
        access_token,
        str(account.get("expired") or account.get("expires_at") or ""),
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "chatgpt_account_id": chatgpt_account_id,
        "chatgpt_user_id": chatgpt_user_id,
        "client_id": account.get("client_id", "app_2SKx67EdpoN0G6j64rFvigXD"),
        "email": user_email,
        "expires_at": expires_at,
        "organization_id": organization_id,
        "plan_type": plan_type or "free",
        "session_token": str(account.get("session_token") or "").strip(),
    }


def build_account_entry(
    account: dict[str, Any],
) -> dict[str, Any]:
    """Build a single sub2api account entry from an account record."""
    email = str(account.get("email") or "").strip()
    source = str(account.get("source_type") or account.get("source") or "registration")

    return {
        "name": email,
        "platform": "openai",
        "type": "oauth",
        "credentials": build_credentials(account),
        "extra": {
            "email": email,
            "auth_provider": "chatgpt2api",
            "source": source,
            "openai_oauth_responses_websockets_v2_enabled": False,
            "openai_oauth_responses_websockets_v2_mode": "off",
            "privacy_mode": str(account.get("privacy_mode") or "training_off"),
        },
        "concurrency": 10,
        "priority": 1,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }


def build_sub2api_bundle(
    accounts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a complete sub2api bundle from a list of account records.

    Args:
        accounts: List of account dicts (from registration or login)

    Returns:
        Sub2API bundle dict ready for JSON serialization
    """
    return {
        "exported_at": _now_iso(),
        "proxies": [],
        "accounts": [build_account_entry(acc) for acc in accounts],
    }


def export_sub2api_json(
    accounts: list[dict[str, Any]],
    output_path: str | Path | None = None,
) -> tuple[str, str]:
    """Export accounts as sub2api bundle JSON.

    Args:
        accounts: List of account records
        output_path: Path to write. If None, auto-generates as
                     sub2api-YYYYMMDD-HHMMSS.json in cwd.

    Returns:
        (JSON string of the sub2api bundle, absolute output path)
    """
    bundle = build_sub2api_bundle(accounts)
    json_str = json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"

    if output_path is None:
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_path = Path(f"sub2api-{timestamp}.json")

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_str, encoding="utf-8")

    return json_str, str(path.resolve())


def export_single_account_json(
    account: dict[str, Any],
    output_path: str | Path | None = None,
) -> str:
    """Export a single account as a CPA-style JSON (for debugging)."""
    creds = build_credentials(account)
    record = {
        "type": "codex",
        "email": str(account.get("email") or ""),
        "expired": creds.get("expires_at", ""),
        "id_token": creds.get("id_token", ""),
        "account_id": creds.get("chatgpt_account_id", ""),
        "disabled": False,
        "access_token": creds.get("access_token", ""),
        "session_token": creds.get("session_token", ""),
        "last_refresh": _now_iso(),
        "refresh_token": creds.get("refresh_token", ""),
        "plan_type": creds.get("plan_type", ""),
    }
    json_str = json.dumps(record, ensure_ascii=False, indent=2) + "\n"

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(json_str, encoding="utf-8")

    return json_str
