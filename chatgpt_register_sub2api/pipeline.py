"""Pipeline orchestrator — wires register → join → refresh/check → export.

The complete flow for one account:
  [1] Register account → get personal-scope tokens
  [2] Join parent K12 workspace → auto-accepted
  [3] Refresh/check account info, or explicit Team re-login when enabled
  [4] Export usable tokens as sub2api JSON

Each account proceeds independently through all 4 stages.
Results are written to registered_accounts.json after each success.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chatgpt_register_sub2api.register.mail_provider import configured_mailboxes
from chatgpt_register_sub2api.register.registrar import register_worker
from chatgpt_register_sub2api.workspace.joiner import join_workspaces
from chatgpt_register_sub2api.login.login_flow import (
    PasswordRequiredError,
    re_login_email_otp_for_team_token,
    re_login_for_team_token,
)
from chatgpt_register_sub2api.export.formats import (
    count_exported_json,
    export_accounts_json,
    export_format_from_config,
    output_filename_from_config,
)
from chatgpt_register_sub2api.utils.proxy import normalize_proxy_url
from chatgpt_register_sub2api.workspace_state import (
    claim_workspace_email,
    get_workspace_email_state,
    list_workspace_entries,
    set_workspace_email_state,
    workspace_email_available,
    workspace_state_path,
)

logger = logging.getLogger(__name__)

WORKSPACE_PLAN_TYPES = {
    "business",
    "education",
    "edu",
    "edu_plus",
    "edu_pro",
    "enterprise",
    "free_workspace",
    "k12",
    "quorum",
    "sci",
    "self_serve_business_usage_based",
    "team",
}
DEFAULT_WORKSPACE_EXPORT_PLAN = "k12"
DEFAULT_HEALTH_CHECK_ENDPOINT = "models"
HEALTH_CHECK_ENDPOINTS = {
    "check": "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
    "models": "https://chatgpt.com/backend-api/models",
}
TOKEN_INVALID_MARKERS = (
    "token_invalidated",
    "authentication token has been invalidated",
    "please try signing in again",
)


class PipelineCancelled(RuntimeError):
    """Raised when a WebUI job asks a running pipeline to stop."""


def _cancel_requested(cancel_event: threading.Event | None = None) -> bool:
    return bool(cancel_event and cancel_event.is_set())


def _raise_if_cancelled(cancel_event: threading.Event | None = None) -> None:
    if _cancel_requested(cancel_event):
        raise PipelineCancelled("Task cancelled by user")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def create_run_output_dir(config: dict[str, Any], count: int | None = None) -> Path:
    """Create a timestamped output folder for a full pipeline run."""
    config_dir = Path(config.get("_config_dir", "."))
    output_cfg = config.get("output", {})
    if not isinstance(output_cfg, dict):
        output_cfg = {}
    runs_dir_value = str(output_cfg.get("runs_dir") or "runs").strip() or "runs"
    runs_dir = Path(runs_dir_value)
    if not runs_dir.is_absolute():
        runs_dir = config_dir / runs_dir

    planned_count = _positive_int(
        count if count is not None else config.get("registration", {}).get("total"),
        1,
    )
    stem = f"{_timestamp()}_{planned_count}_accounts"
    run_dir = runs_dir / stem
    suffix = 2
    while run_dir.exists():
        run_dir = runs_dir / f"{stem}-{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _mail_config_with_proxy(config: dict[str, Any]) -> dict[str, Any]:
    mail_cfg = dict(config.get("mail", {}))
    proxy = str(config.get("proxy", {}).get("url", "")).strip()
    if proxy and not mail_cfg.get("proxy"):
        mail_cfg["proxy"] = proxy
    workspace_ids = _string_list(config.get("workspace", {}).get("ids", []))
    if workspace_ids:
        mail_cfg["workspace_id"] = workspace_ids[0]
        mail_cfg["workspace_state_file"] = str(workspace_state_path(config))
    return mail_cfg


def _primary_workspace_id(config: dict[str, Any]) -> str:
    workspace_ids = _string_list(config.get("workspace", {}).get("ids", []))
    return workspace_ids[0] if workspace_ids else ""


def workspace_state_entries(
    config: dict[str, Any],
    workspace_id: str = "",
) -> list[dict[str, Any]]:
    return list_workspace_entries(
        workspace_state_path(config),
        workspace_id or _primary_workspace_id(config),
    )


def preview_mailbox_candidates(
    config: dict[str, Any],
    count: int | None = None,
    workspace_id: str = "",
) -> list[dict[str, Any]]:
    target_workspace = workspace_id or _primary_workspace_id(config)
    state_file = workspace_state_path(config)
    candidates: list[dict[str, Any]] = []
    for item in configured_mailboxes(_mail_config_with_proxy(config)):
        address = str(item.get("address") or "").strip()
        available = (
            not target_workspace
            or workspace_email_available(state_file, target_workspace, address)
        )
        row = {
            "provider": item.get("provider", ""),
            "label": item.get("label", ""),
            "address": address,
            "base_address": item.get("base_address", address),
            "alias_index": item.get("alias_index"),
            "workspace_id": target_workspace,
            "available": available,
        }
        candidates.append(row)
        if count is not None and len([c for c in candidates if c["available"]]) >= count:
            break
    return candidates


def _resolve_export_output_path(
    config: dict[str, Any],
    output_file: str | Path | None = None,
) -> Path:
    if output_file:
        path = Path(output_file)
    else:
        configured = output_filename_from_config(config)
        path = Path(configured) if configured else Path(f"sub2api-{_timestamp()}.json")

    if not path.is_absolute():
        path = Path(config.get("_config_dir", ".")) / path
    return path


def _positive_int(value: Any, default: int = 1) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


def _nonnegative_float(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return max(0.0, float(default))


def _bool_config_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _parallel_threads(config: dict[str, Any], key: str, default: int = 1) -> int:
    parallel_cfg = config.get("parallel", {})
    if not isinstance(parallel_cfg, dict):
        parallel_cfg = {}
    return _positive_int(parallel_cfg.get(key, default), default)


def _bounded_workers(requested: int, item_count: int) -> int:
    return max(1, min(_positive_int(requested, 1), max(1, item_count)))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, tuple):
        raw_items = list(value)
    elif value is None:
        raw_items = []
    else:
        raw_items = [value]

    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def load_accounts(path: Path) -> list[dict[str, Any]]:
    """Load registered accounts from JSON file."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_accounts(path: Path, accounts: list[dict[str, Any]]) -> None:
    """Save accounts to JSON file (atomic write)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(accounts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _create_http_session(proxy: str = ""):
    from curl_cffi import requests

    kwargs = {"impersonate": "chrome", "verify": True}
    proxy_url = normalize_proxy_url(proxy)
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return requests.Session(**kwargs)


def _is_workspace_plan(plan: Any) -> bool:
    return str(plan or "").strip().lower() in WORKSPACE_PLAN_TYPES


def _response_excerpt(resp: Any, limit: int = 300) -> str:
    text = str(getattr(resp, "text", "") or "")
    if not text:
        return ""
    return text.replace("\r", "\\r").replace("\n", "\\n")[:limit]


def _token_invalidated_detail(detail: str) -> bool:
    lowered = str(detail or "").lower()
    return any(marker in lowered for marker in TOKEN_INVALID_MARKERS)


def _account_email(account: dict[str, Any]) -> str:
    return str(account.get("email") or account.get("name") or "?")


def _fetch_account_context(
    session,
    access_token: str,
    workspace_id: str = "",
) -> dict[str, str]:
    resp = session.get(
        "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        detail = _response_excerpt(resp)
        suffix = f", body={detail}" if detail else ""
        if resp.status_code in (401, 403) and _token_invalidated_detail(detail):
            raise RuntimeError(
                f"check API token invalidated: HTTP {resp.status_code}{suffix}"
            )
        raise RuntimeError(f"check API failed: HTTP {resp.status_code}{suffix}")

    try:
        data = resp.json() if resp.text else {}
    except Exception as error:
        detail = _response_excerpt(resp)
        suffix = f", body={detail}" if detail else ""
        raise RuntimeError(
            f"check API returned non-JSON: HTTP {resp.status_code}{suffix}"
        ) from error
    candidates = _extract_account_contexts(data)
    target_workspace = str(workspace_id or "").strip()
    selected = None
    if target_workspace:
        selected = next(
            (
                item
                for item in candidates
                if target_workspace in item.get("_ids", set())
            ),
            None,
        )
    if selected is None:
        selected = next((item for item in candidates if item.get("_default")), None)
    if selected is None and candidates:
        selected = candidates[0]
    if selected is None:
        return {
            "plan_type": "",
            "chatgpt_account_id": "",
            "account_user_role": "",
        }

    return {
        "plan_type": str(selected.get("plan_type") or "").strip(),
        "chatgpt_account_id": str(selected.get("chatgpt_account_id") or "").strip(),
        "account_user_role": str(selected.get("account_user_role") or "").strip(),
    }


def _extract_account_contexts(data: Any) -> list[dict[str, Any]]:
    """Extract account contexts from /accounts/check across known shapes."""
    contexts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(raw: Any, is_default: bool = False) -> None:
        if not isinstance(raw, dict):
            return
        wrapper = raw
        account = raw.get("account") if isinstance(raw.get("account"), dict) else raw
        ids = {
            str(value).strip()
            for source in (wrapper, account)
            for key in ("id", "account_id", "workspace_id")
            for value in [source.get(key)]
            if value
        }
        plan = str(account.get("plan_type") or wrapper.get("plan_type") or "").strip()
        account_id = str(
            account.get("account_id")
            or wrapper.get("account_id")
            or account.get("id")
            or wrapper.get("id")
            or ""
        ).strip()
        role = str(
            account.get("account_user_role")
            or wrapper.get("account_user_role")
            or wrapper.get("role")
            or ""
        ).strip()
        if not (plan or account_id or role or ids):
            return
        key = (account_id, plan, role, ",".join(sorted(ids)))
        if key in seen:
            return
        seen.add(key)
        contexts.append(
            {
                "plan_type": plan,
                "chatgpt_account_id": account_id,
                "account_user_role": role,
                "_default": is_default,
                "_ids": ids,
            }
        )

    accounts = data.get("accounts") if isinstance(data, dict) else None
    if isinstance(accounts, dict):
        default = accounts.get("default")
        if isinstance(default, dict):
            add(default, is_default=True)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            add(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(accounts if accounts is not None else data)
    return contexts


def _apply_account_context(
    account: dict[str, Any],
    context: dict[str, str],
    *,
    prefix: str = "",
    overwrite_default: bool = False,
) -> None:
    plan = context.get("plan_type", "")
    account_id = context.get("chatgpt_account_id", "")
    role = context.get("account_user_role", "")

    if plan:
        account[f"{prefix}plan_type"] = plan
        if overwrite_default:
            account["plan_type"] = plan
    if account_id:
        account[f"{prefix}chatgpt_account_id"] = account_id
        if overwrite_default:
            account["chatgpt_account_id"] = account_id
    if role:
        account[f"{prefix}account_user_role"] = role
        if overwrite_default:
            account["account_user_role"] = role


def _active_workspace_id(
    account: dict[str, Any],
    workspace_ids: list[str],
) -> str:
    if not account.get("workspace_membership_active"):
        return ""
    join_results = account.get("join_results")
    if isinstance(join_results, list):
        for result in join_results:
            if not isinstance(result, dict):
                continue
            ws_id = str(result.get("workspace_id") or "").strip()
            if (
                ws_id
                and result.get("ok")
                and result.get("membership_active", True)
                and (not workspace_ids or ws_id in workspace_ids)
            ):
                return ws_id
    return workspace_ids[0] if workspace_ids else ""


def _apply_workspace_export_context(
    account: dict[str, Any],
    workspace_id: str,
    plan_type: str = DEFAULT_WORKSPACE_EXPORT_PLAN,
) -> None:
    if not workspace_id:
        return
    account["workspace_export_status"] = "ok"
    account["workspace_plan_type"] = plan_type
    account["workspace_chatgpt_account_id"] = workspace_id
    account["workspace_account_user_role"] = (
        str(account.get("workspace_account_user_role") or "")
        or str(account.get("account_user_role") or "")
        or "member"
    )
    account["plan_type"] = plan_type
    account["chatgpt_account_id"] = workspace_id
    account["account_user_role"] = account["workspace_account_user_role"]


def _mark_join_verified_by_check(
    account: dict[str, Any],
    workspace_id: str,
) -> None:
    if not workspace_id:
        return
    account["join_status"] = "ok"
    account["workspace_membership_active"] = True
    results = account.get("join_results")
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, dict):
                continue
            if str(result.get("workspace_id") or "").strip() != workspace_id:
                continue
            result["ok"] = True
            result["membership_active"] = True
            result["membership_detail"] = "verified by accounts/check"
            result["verified_by"] = "accounts_check"


def _has_verified_workspace_context(account: dict[str, Any]) -> bool:
    return (
        bool(account.get("access_token"))
        and bool(account.get("chatgpt_account_id"))
        and _is_workspace_plan(account.get("plan_type"))
        and account.get("refresh_status") != "failed"
        and account.get("export_health_status") != "failed"
    )


def _has_auth_invalid_join_result(account: dict[str, Any]) -> bool:
    join_results = account.get("join_results")
    if not isinstance(join_results, list):
        return False
    for result in join_results:
        if not isinstance(result, dict):
            continue
        detail = " ".join(
            str(result.get(key) or "")
            for key in ("error", "body", "membership_detail")
        )
        if _token_invalidated_detail(detail):
            return True
    return False


def _mark_refresh_status(
    account: dict[str, Any],
    status: str,
    detail: str = "",
) -> None:
    account["refresh_status"] = status
    account["refresh_checked_at"] = _now()
    if detail:
        account["refresh_error"] = detail
    else:
        account.pop("refresh_error", None)


def _export_health_url(endpoint: str) -> str:
    selected = str(endpoint or DEFAULT_HEALTH_CHECK_ENDPOINT).strip()
    if selected.startswith("http://") or selected.startswith("https://"):
        return selected
    return HEALTH_CHECK_ENDPOINTS.get(selected.lower(), HEALTH_CHECK_ENDPOINTS["models"])


def _check_export_token_health(
    session,
    account: dict[str, Any],
    sub2api_cfg: dict[str, Any],
) -> tuple[bool, str]:
    access_token = str(account.get("access_token") or "").strip()
    if not access_token:
        return False, "missing access_token"

    endpoint_name = str(
        sub2api_cfg.get("health_check_endpoint") or DEFAULT_HEALTH_CHECK_ENDPOINT
    ).strip()
    endpoint_url = _export_health_url(endpoint_name)
    timeout = _positive_int(sub2api_cfg.get("health_check_timeout"), 30)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "oai-language": "en-US",
    }
    account_id = str(account.get("chatgpt_account_id") or "").strip()
    if account_id:
        headers["chatgpt-account-id"] = account_id

    if endpoint_name.lower() == "check":
        context = _fetch_account_context(session, access_token, account_id)
        if not context.get("chatgpt_account_id"):
            return False, "check API returned no account context"
        if _is_workspace_plan(account.get("plan_type")) and not _is_workspace_plan(
            context.get("plan_type")
        ):
            return False, f"check API resolved non-workspace plan={context.get('plan_type') or '?'}"
        return True, "ok"

    resp = session.get(endpoint_url, headers=headers, timeout=timeout)
    detail = _response_excerpt(resp)
    if resp.status_code == 200:
        return True, "ok"
    if resp.status_code in (401, 403) and _token_invalidated_detail(detail):
        return False, f"token invalidated: HTTP {resp.status_code}, body={detail}"
    suffix = f", body={detail}" if detail else ""
    return False, f"health check failed: HTTP {resp.status_code}{suffix}"


def _refresh_oauth_tokens_for_account(
    session,
    account: dict[str, Any],
) -> tuple[bool, str]:
    refresh_token = str(account.get("refresh_token") or "").strip()
    if not refresh_token:
        return False, "missing refresh_token"
    resp = session.post(
        "https://auth.openai.com/oauth/token",
        data={
            "client_id": "app_2SKx67EdpoN0G6j64rFvigXD",
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        detail = _response_excerpt(resp)
        return (
            False,
            f"token refresh failed: HTTP {resp.status_code}"
            f"{', body=' + detail if detail else ''}",
        )
    try:
        data = resp.json()
    except Exception:
        return False, f"token refresh returned non-JSON: {_response_excerpt(resp)}"
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        return False, "token refresh returned no access_token"
    account["access_token"] = access_token
    new_refresh = str(data.get("refresh_token") or "").strip()
    if new_refresh:
        account["refresh_token"] = new_refresh
    id_token = str(data.get("id_token") or "").strip()
    if id_token:
        account["id_token"] = id_token
    return True, "ok"


def _recover_export_token(
    config: dict[str, Any],
    session,
    original: dict[str, Any],
    export: dict[str, Any],
    last_error: str,
) -> tuple[bool, str]:
    if not _token_invalidated_detail(last_error):
        return False, last_error
    if not hasattr(session, "post"):
        return False, last_error

    try:
        ok, detail = _refresh_oauth_tokens_for_account(session, original)
    except Exception as error:
        ok = False
        detail = str(error)
    if ok:
        for key in ("access_token", "refresh_token", "id_token"):
            if original.get(key):
                export[key] = original[key]
        return True, "refreshed"

    existing_cfg = config.get("existing_login", {})
    if not isinstance(existing_cfg, dict):
        existing_cfg = {}
    if str(existing_cfg.get("mode") or "email_otp").strip().lower() != "email_otp":
        return False, detail

    email = _account_email(original)
    if not email or email == "?":
        return False, detail
    mail_cfg = _mail_config_with_proxy(config)
    if not configured_mailboxes(mail_cfg):
        return False, detail
    try:
        proxy_cfg = config.get("proxy", {})
        tokens = re_login_email_otp_for_team_token(
            email=email,
            mail_config=mail_cfg,
            proxy=str(proxy_cfg.get("url", "")).strip(),
            flaresolverr_url=str(proxy_cfg.get("flaresolverr_url", "")).strip(),
            workspace_id="",
        )
    except Exception as error:
        return False, f"{detail}; otp relogin failed: {error}"

    for key in ("access_token", "refresh_token", "id_token"):
        if tokens.get(key):
            original[key] = tokens[key]
            export[key] = tokens[key]
    original["source_type"] = original.get("source_type") or "existing_login"
    return True, "otp_relogin"


def _filter_healthy_export_accounts(
    config: dict[str, Any],
    account_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    sub2api_cfg = config.get("sub2api", {})
    if not isinstance(sub2api_cfg, dict):
        sub2api_cfg = {}
    if not _bool_config_value(sub2api_cfg.get("health_check"), False):
        return account_pairs

    delay = _nonnegative_float(sub2api_cfg.get("health_check_delay_seconds"), 0.0)
    if delay:
        logger.info(f"Waiting {delay:.1f}s before export health check")
        time.sleep(delay)

    proxy = str(config.get("proxy", {}).get("url", "")).strip()
    retries = _positive_int(sub2api_cfg.get("health_check_retries"), 2)
    backoff_ms = _positive_int(sub2api_cfg.get("health_check_backoff_ms"), 3000)
    healthy: list[tuple[dict[str, Any], dict[str, Any]]] = []
    session = None
    try:
        session = _create_http_session(proxy)
        for original, export in account_pairs:
            email = _account_email(export)
            last_error = ""
            ok = False
            for attempt in range(1, retries + 1):
                try:
                    ok, last_error = _check_export_token_health(
                        session,
                        export,
                        sub2api_cfg,
                    )
                except Exception as error:
                    ok = False
                    last_error = str(error)
                if ok:
                    break
                if attempt < retries:
                    time.sleep(backoff_ms * attempt / 1000.0)
            if not ok and _token_invalidated_detail(last_error):
                recovered, recovery_detail = _recover_export_token(
                    config,
                    session,
                    original,
                    export,
                    last_error,
                )
                if recovered:
                    try:
                        ok, last_error = _check_export_token_health(
                            session,
                            export,
                            sub2api_cfg,
                        )
                    except Exception as error:
                        ok = False
                        last_error = str(error)
                    if ok:
                        logger.info(f"[{email}] Export health recovered via {recovery_detail}")
                elif recovery_detail:
                    last_error = recovery_detail

            checked_at = _now()
            original["export_health_checked_at"] = checked_at
            export["export_health_checked_at"] = checked_at
            if ok:
                original["export_health_status"] = "ok"
                original.pop("export_health_error", None)
                export["export_health_status"] = "ok"
                export.pop("export_health_error", None)
                healthy.append((original, export))
            else:
                original["export_health_status"] = "failed"
                original["export_health_error"] = last_error
                export["export_health_status"] = "failed"
                export["export_health_error"] = last_error
                logger.warning(f"[{email}] Export health check failed: {last_error}")
    finally:
        if session:
            session.close()

    logger.info(
        f"Export health check passed: {len(healthy)}/{len(account_pairs)} accounts"
    )
    return healthy


def _count_exported_accounts(json_str: str) -> int:
    return count_exported_json(json_str)


def _verify_workspace_membership(
    session,
    access_token: str,
    workspace_id: str,
) -> tuple[bool, str]:
    resp = session.get(
        f"https://chatgpt.com/backend-api/accounts/{workspace_id}/users",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if resp.status_code == 200:
        return True, ""
    detail = (resp.text or "")[:300] if hasattr(resp, "text") else ""
    return False, detail or f"HTTP {resp.status_code}"


# ── Pipeline stages ─────────────────────────────────────────────────


def run_register(
    config: dict[str, Any],
    accounts_file: Path,
    count: int | None = None,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Stage 1: Register N ChatGPT accounts.

    Returns list of newly registered account records.
    """
    reg_cfg = config.get("registration", {})
    proxy_cfg = config.get("proxy", {})

    total = (
        _positive_int(count, 10)
        if count is not None
        else _positive_int(reg_cfg.get("total", 10), 10)
    )
    threads = _positive_int(reg_cfg.get("threads", 3), 3)
    proxy = str(proxy_cfg.get("url", "")).strip()
    flaresolverr_url = str(proxy_cfg.get("flaresolverr_url", "")).strip()
    mail_cfg = _mail_config_with_proxy(config)

    logger.info(f"Starting registration: {total} accounts, {threads} threads")
    if proxy:
        logger.info(f"Proxy: {proxy}")
    if flaresolverr_url:
        logger.info(f"FlareSolverr: {flaresolverr_url}")

    results: list[dict[str, Any]] = []
    existing = load_accounts(accounts_file)
    success_count = 0
    fail_count = 0

    _raise_if_cancelled(cancel_event)
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {
            executor.submit(
                register_worker,
                index=i,
                proxy=proxy,
                flaresolverr_url=flaresolverr_url,
                mail_config=mail_cfg,
            ): i
            for i in range(1, total + 1)
        }
        pending = set(futures)

        while pending:
            if _cancel_requested(cancel_event):
                for future in pending:
                    future.cancel()
                logger.warning("Registration cancelled — waiting for running workers to finish")
                raise PipelineCancelled("Registration cancelled by user")

            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                if future.cancelled():
                    continue
                result = future.result()
                if result["ok"]:
                    success_count += 1
                    account = result["result"]
                    results.append(account)
                    existing.append(account)
                    save_accounts(accounts_file, existing)
                    logger.info(
                        f"[{result['index']}/{total}] ✓ {account['email']} "
                        f"({result.get('cost_seconds', 0):.1f}s)"
                    )
                else:
                    fail_count += 1
                    logger.warning(
                        f"[{result['index']}/{total}] ✗ {result.get('error', 'unknown')}"
                    )

    logger.info(
        f"Registration complete: {success_count} success, {fail_count} failed"
    )
    return results


def run_login_existing(
    config: dict[str, Any],
    accounts_file: Path,
    count: int | None = None,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Log in existing ChatGPT accounts with email OTP and save fresh tokens."""
    proxy_cfg = config.get("proxy", {})
    proxy = str(proxy_cfg.get("url", "")).strip()
    flaresolverr_url = str(proxy_cfg.get("flaresolverr_url", "")).strip()
    mail_cfg = _mail_config_with_proxy(config)
    workspace_id = _primary_workspace_id(config)
    state_file = workspace_state_path(config)
    total = _positive_int(count, 10) if count is not None else _positive_int(
        config.get("registration", {}).get("total", 10),
        10,
    )
    threads = _bounded_workers(
        _parallel_threads(config, "login_threads", 1),
        total,
    )

    candidates = [
        item for item in configured_mailboxes(mail_cfg)
        if not workspace_id
        or workspace_email_available(state_file, workspace_id, item.get("address", ""))
    ][:total]
    logger.info(
        f"Logging in {len(candidates)} existing account(s) with email OTP, "
        f"{threads} worker(s)"
    )

    existing = load_accounts(accounts_file)
    results: list[dict[str, Any]] = []
    lock = threading.Lock()

    def _login_one(candidate: dict[str, Any]) -> dict[str, Any] | None:
        _raise_if_cancelled(cancel_event)
        email = str(candidate.get("address") or "").strip()
        if not email:
            return None
        if workspace_id and not claim_workspace_email(
            state_file,
            workspace_id,
            email,
            mode="login_existing",
            extra={
                "provider": candidate.get("provider", ""),
                "base_address": candidate.get("base_address", email),
                "alias_index": candidate.get("alias_index"),
            },
        ):
            logger.info(f"[{email}] Workspace already processed — skipping login")
            return None
        try:
            tokens = re_login_email_otp_for_team_token(
                email=email,
                mail_config=mail_cfg,
                proxy=proxy,
                flaresolverr_url=flaresolverr_url,
                workspace_id="",
            )
            account = {
                "email": email,
                "password": "",
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "id_token": tokens["id_token"],
                "source_type": "existing_login",
                "created_at": tokens.get("created_at") or _now(),
            }
            with lock:
                existing.append(account)
                results.append(account)
                save_accounts(accounts_file, existing)
            if workspace_id:
                set_workspace_email_state(
                    state_file,
                    workspace_id,
                    email,
                    "logged_in",
                    mode="login_existing",
                    extra={
                        "provider": candidate.get("provider", ""),
                        "base_address": candidate.get("base_address", email),
                        "alias_index": candidate.get("alias_index"),
                    },
                )
            logger.info(f"[{email}] ✓ Existing account logged in")
            return account
        except PasswordRequiredError as error:
            if workspace_id:
                set_workspace_email_state(
                    state_file,
                    workspace_id,
                    email,
                    "otp_login_unavailable",
                    mode="login_existing",
                    reason=str(error),
                )
            logger.warning(f"[{email}] ✗ OTP login unavailable: {error}")
        except Exception as error:
            if workspace_id:
                set_workspace_email_state(
                    state_file,
                    workspace_id,
                    email,
                    "failed",
                    mode="login_existing",
                    reason=str(error),
                )
            logger.warning(f"[{email}] ✗ Existing login failed: {error}")
        return None

    if threads == 1 or len(candidates) <= 1:
        for candidate in candidates:
            _raise_if_cancelled(cancel_event)
            _login_one(candidate)
    else:
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = [executor.submit(_login_one, candidate) for candidate in candidates]
            pending = set(futures)
            while pending:
                if _cancel_requested(cancel_event):
                    for future in pending:
                        future.cancel()
                    logger.warning("Existing-account login cancelled")
                    raise PipelineCancelled("Existing-account login cancelled by user")
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for future in done:
                    if not future.cancelled():
                        future.result()

    logger.info(
        f"Existing account login complete: {len(results)} success, "
        f"{len(candidates) - len(results)} failed/skipped"
    )
    return results


def run_join_workspace(
    config: dict[str, Any],
    accounts: list[dict[str, Any]],
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Stage 2: Join each account to the K12 parent workspace.

    Modifies account records in-place with join status.
    """
    ws_cfg = config.get("workspace", {})
    if not ws_cfg.get("enabled", True):
        logger.info("Workspace join disabled — skipping")
        return accounts

    workspace_ids = _string_list(ws_cfg.get("ids", []))
    if not workspace_ids:
        logger.warning("No workspace IDs configured — skipping join")
        return accounts

    route = str(ws_cfg.get("route", "k12_request")).strip() or "k12_request"
    max_retries = _positive_int(ws_cfg.get("max_retries", 3), 3)
    retry_backoff = _positive_int(ws_cfg.get("retry_backoff_ms", 5000), 5000)
    proxy = str(config.get("proxy", {}).get("url", "")).strip()
    threads = _bounded_workers(
        _parallel_threads(config, "join_threads", 3),
        len(accounts),
    )

    logger.info(
        f"Joining {len(accounts)} accounts to {len(workspace_ids)} "
        f"workspace(s), {threads} worker(s)"
    )
    state_file = workspace_state_path(config)

    def _join_one(account: dict[str, Any]) -> dict[str, Any]:
        _raise_if_cancelled(cancel_event)
        email = account.get("email", "?")
        access_token = account.get("access_token", "")
        if not access_token:
            logger.warning(f"[{email}] No access_token — skipping join")
            account["join_status"] = "skipped"
            return account
        if len(workspace_ids) == 1:
            entry = get_workspace_email_state(state_file, workspace_ids[0], email)
            if isinstance(entry, dict) and str(entry.get("state") or "") == "exported":
                logger.info(f"[{email}] Already exported for workspace — skipping join")
                account["join_status"] = "skipped"
                return account

        results = join_workspaces(
            access_token=access_token,
            workspace_ids=workspace_ids,
            route=route,
            max_retries=max_retries,
            retry_backoff_ms=retry_backoff,
            proxy=proxy,
        )

        request_ok = all(r["ok"] for r in results)
        membership_ok = False

        verify_session = None
        try:
            verify_session = _create_http_session(proxy)
            membership_map: dict[str, tuple[bool, str]] = {}
            for ws_id in workspace_ids:
                membership_map[ws_id] = _verify_workspace_membership(
                    verify_session,
                    access_token,
                    ws_id,
                )
            for result in results:
                result["request_ok"] = bool(result.get("ok"))
                active, detail = membership_map.get(
                    str(result.get("workspace_id") or ""),
                    (False, "workspace verification missing"),
                )
                result["membership_active"] = active
                if active and not result.get("ok"):
                    result["ok"] = True
                    result["membership_detail"] = "verified by membership check"
                elif detail:
                    result["membership_detail"] = detail
            membership_ok = all(active for active, _ in membership_map.values())
        except Exception as e:
            membership_ok = False
            for result in results:
                result["request_ok"] = bool(result.get("ok"))
                result["membership_active"] = False
                result["membership_detail"] = f"verification error: {e}"
        finally:
            if verify_session:
                verify_session.close()

        all_ok = membership_ok or request_ok
        account["join_status"] = "ok" if all_ok else "failed"
        account["join_results"] = results
        account["workspace_membership_active"] = membership_ok

        if all_ok:
            for ws_id in workspace_ids:
                set_workspace_email_state(
                    state_file,
                    ws_id,
                    email,
                    "joined",
                    mode=str(account.get("source_type") or "account"),
                    extra={"join_status": "ok"},
                )
            logger.info(f"[{email}] ✓ Joined {len(workspace_ids)} workspace(s)")
        else:
            errors = [
                r.get("error")
                or r.get("membership_detail")
                or "workspace membership not active"
                for r in results
                if (not r["ok"]) or not r.get("membership_active", request_ok)
            ]
            for ws_id in workspace_ids:
                set_workspace_email_state(
                    state_file,
                    ws_id,
                    email,
                    "failed",
                    mode=str(account.get("source_type") or "account"),
                    reason=", ".join(str(e) for e in errors)[:300],
                    extra={"join_status": "failed"},
                )
            logger.warning(f"[{email}] ✗ Join failed: {', '.join(errors)}")

        return account

    if threads == 1 or len(accounts) <= 1:
        for account in accounts:
            _raise_if_cancelled(cancel_event)
            _join_one(account)
        return accounts

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(_join_one, account) for account in accounts]
        pending = set(futures)
        while pending:
            if _cancel_requested(cancel_event):
                for future in pending:
                    future.cancel()
                logger.warning("Workspace join cancelled")
                raise PipelineCancelled("Workspace join cancelled by user")
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                if not future.cancelled():
                    future.result()

    return accounts


def run_re_login(
    config: dict[str, Any],
    accounts: list[dict[str, Any]],
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Stage 3: Re-login each account with Team space selection.

    Gets team-scoped tokens for accounts that successfully joined.
    NOTE: This step requires browser-based OAuth login flow and is
    currently skipped by default. Use registration tokens directly.
    """
    ws_cfg = config.get("workspace", {})
    re_login_enabled = ws_cfg.get("re_login_enabled", False)

    if not re_login_enabled:
        logger.info("Team re-login disabled — using registration tokens for export")
        for account in accounts:
            account["team_login_status"] = "skipped"
        return accounts

    proxy_cfg = config.get("proxy", {})
    proxy = str(proxy_cfg.get("url", "")).strip()
    flaresolverr_url = str(proxy_cfg.get("flaresolverr_url", "")).strip()
    mail_cfg = _mail_config_with_proxy(config)
    workspace_ids = _string_list(ws_cfg.get("ids", []))
    threads = _bounded_workers(
        _parallel_threads(config, "login_threads", 1),
        len(accounts),
    )

    logger.info(
        f"Re-logging {len(accounts)} accounts for team-scoped tokens, "
        f"{threads} worker(s)"
    )

    def _login_one(account: dict[str, Any]) -> dict[str, Any]:
        _raise_if_cancelled(cancel_event)
        email = account.get("email", "")
        password = account.get("password", "")
        join_status = account.get("join_status", "")

        if join_status != "ok":
            logger.info(f"[{email}] Join failed/skipped — skipping re-login")
            account["team_login_status"] = "skipped"
            return account

        if not email or not password:
            logger.warning(f"[{email}] Missing email or password — skipping re-login")
            account["team_login_status"] = "skipped"
            return account

        for key in (
            "team_access_token",
            "team_refresh_token",
            "team_id_token",
            "team_plan_type",
            "team_chatgpt_account_id",
            "team_account_user_role",
            "team_login_error",
        ):
            account.pop(key, None)

        try:
            logger.info(f"[{email}] Starting team re-login")
            team_tokens = re_login_for_team_token(
                email=email,
                password=password,
                mail_config=mail_cfg,
                proxy=proxy,
                flaresolverr_url=flaresolverr_url,
                workspace_id=workspace_ids[0] if workspace_ids else "",
            )

            # Store team-scoped tokens in a separate field
            account["team_access_token"] = team_tokens["access_token"]
            account["team_refresh_token"] = team_tokens["refresh_token"]
            account["team_id_token"] = team_tokens["id_token"]

            check_session = None
            try:
                check_session = _create_http_session(proxy)
                context = _fetch_account_context(
                    check_session,
                    team_tokens["access_token"],
                    workspace_id=workspace_ids[0] if workspace_ids else "",
                )
                _apply_account_context(
                    account,
                    context,
                    prefix="team_",
                    overwrite_default=True,
                )

                if not _is_workspace_plan(context.get("plan_type", "")):
                    raise RuntimeError(
                        f"team token still resolved to personal scope "
                        f"(plan={context.get('plan_type') or 'unknown'})"
                    )
            finally:
                if check_session:
                    check_session.close()

            account["team_login_status"] = "ok"
            logger.info(
                f"[{email}] ✓ Team login successful "
                f"(plan={account.get('team_plan_type', account.get('plan_type', '?'))})"
            )
        except Exception as e:
            logger.warning(f"[{email}] ✗ Team login failed: {e}")
            account["team_login_status"] = "failed"
            account["team_login_error"] = str(e)

        return account

    if threads == 1 or len(accounts) <= 1:
        for account in accounts:
            _raise_if_cancelled(cancel_event)
            _login_one(account)
        return accounts

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(_login_one, account) for account in accounts]
        pending = set(futures)
        while pending:
            if _cancel_requested(cancel_event):
                for future in pending:
                    future.cancel()
                logger.warning("Team re-login cancelled")
                raise PipelineCancelled("Team re-login cancelled by user")
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                if not future.cancelled():
                    future.result()

    return accounts


def run_refresh_tokens(
    config: dict[str, Any],
    accounts: list[dict[str, Any]],
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Refresh access tokens and enrich with workspace info from check API.

    After joining a workspace, refreshing the token ensures the token
    is valid for the current context.  Then we call /accounts/check
    to get the real plan_type and account_id (the JWT doesn't carry
    workspace claims).
    """
    proxy = str(config.get("proxy", {}).get("url", "")).strip()
    ws_cfg = config.get("workspace", {})
    workspace_ids = _string_list(ws_cfg.get("ids", []))
    workspace_plan = str(
        ws_cfg.get("export_plan_type") or DEFAULT_WORKSPACE_EXPORT_PLAN
    ).strip() or DEFAULT_WORKSPACE_EXPORT_PLAN
    threads = _bounded_workers(
        _parallel_threads(config, "refresh_threads", 3),
        len(accounts),
    )

    logger.info(
        f"Refreshing tokens and checking account info for {len(accounts)} "
        f"accounts, {threads} worker(s)"
    )
    state_file = workspace_state_path(config)

    def _refresh_one(account: dict[str, Any]) -> dict[str, Any]:
        _raise_if_cancelled(cancel_event)
        email = account.get("email", "")
        rt = account.get("refresh_token", "")

        if not rt:
            logger.warning(f"[{email}] No refresh_token — skipping refresh")
            _mark_refresh_status(account, "failed", "missing refresh_token")
            return account

        session = None
        try:
            session = _create_http_session(proxy)

            # Step 1: Refresh the access token
            resp = session.post(
                "https://auth.openai.com/oauth/token",
                data={
                    "client_id": "app_2SKx67EdpoN0G6j64rFvigXD",
                    "grant_type": "refresh_token",
                    "refresh_token": rt,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception as error:
                    detail = _response_excerpt(resp)
                    raise RuntimeError(
                        f"token refresh returned non-JSON: HTTP {resp.status_code}, "
                        f"body={detail}"
                    ) from error
                new_at = data.get("access_token", "")
                new_rt = data.get("refresh_token", "")
                if new_at:
                    account["access_token"] = new_at
                if new_rt:
                    account["refresh_token"] = new_rt
                logger.info(f"[{email}] Token refreshed")
            else:
                detail = _response_excerpt(resp)
                raise RuntimeError(
                    f"token refresh failed: HTTP {resp.status_code}"
                    f"{', body=' + detail if detail else ''}"
                )

            # Step 2: Call check API to get real plan_type and account_id
            at = account.get("access_token", "")
            if at:
                active_workspace_id = _active_workspace_id(account, workspace_ids)
                context = _fetch_account_context(
                    session,
                    at,
                    workspace_id=active_workspace_id,
                )
                _apply_account_context(account, context)
                if active_workspace_id:
                    _apply_workspace_export_context(
                        account,
                        active_workspace_id,
                        str(account.get("plan_type") or workspace_plan),
                    )
                context_account_id = str(context.get("chatgpt_account_id") or "").strip()
                if (
                    workspace_ids
                    and context_account_id in workspace_ids
                    and _is_workspace_plan(context.get("plan_type", ""))
                ):
                    _mark_join_verified_by_check(account, context_account_id)
                    _apply_workspace_export_context(
                        account,
                        context_account_id,
                        str(context.get("plan_type") or workspace_plan),
                    )
                logger.info(
                    f"[{email}] Check API: "
                    f"plan={context.get('plan_type', '')} "
                    f"account_id={context.get('chatgpt_account_id', '')[:30] or '?'} "
                    f"role={context.get('account_user_role', '')}"
                )
                if account.get("workspace_export_status") == "ok":
                    logger.info(
                        f"[{email}] Workspace export context: "
                        f"plan={account.get('workspace_plan_type')} "
                        f"account_id={account.get('workspace_chatgpt_account_id')}"
                    )
                _mark_refresh_status(account, "ok")
                checked_workspace_id = (
                    str(account.get("workspace_chatgpt_account_id") or "")
                    or active_workspace_id
                    or (workspace_ids[0] if workspace_ids else "")
                )
                if checked_workspace_id:
                    set_workspace_email_state(
                        state_file,
                        checked_workspace_id,
                        email,
                        "checked",
                        mode=str(account.get("source_type") or "account"),
                        extra={
                            "plan_type": account.get("workspace_plan_type")
                            or account.get("plan_type", ""),
                            "chatgpt_account_id": account.get(
                                "workspace_chatgpt_account_id"
                            )
                            or account.get("chatgpt_account_id", ""),
                            "account_user_role": account.get(
                                "workspace_account_user_role"
                            )
                            or account.get("account_user_role", ""),
                            "join_status": account.get("join_status", ""),
                            "join_verified_by": (
                                "accounts_check"
                                if account.get("join_status") == "ok"
                                else ""
                            ),
                        },
                    )
            else:
                _mark_refresh_status(account, "failed", "missing access_token after refresh")

        except Exception as e:
            detail = str(e)
            _mark_refresh_status(account, "failed", detail)
            for ws_id in workspace_ids[:1]:
                set_workspace_email_state(
                    state_file,
                    ws_id,
                    email,
                    "failed",
                    mode=str(account.get("source_type") or "account"),
                    reason=detail,
                    extra={"refresh_status": "failed"},
                )
            logger.warning(f"[{email}] Refresh/check error: {detail}")
        finally:
            if session:
                session.close()

        return account

    if threads == 1 or len(accounts) <= 1:
        for account in accounts:
            _raise_if_cancelled(cancel_event)
            _refresh_one(account)
        return accounts

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(_refresh_one, account) for account in accounts]
        pending = set(futures)
        while pending:
            if _cancel_requested(cancel_event):
                for future in pending:
                    future.cancel()
                logger.warning("Refresh/check cancelled")
                raise PipelineCancelled("Refresh/check cancelled by user")
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                if not future.cancelled():
                    future.result()

    return accounts


def run_export(
    config: dict[str, Any],
    accounts: list[dict[str, Any]],
    output_file: Path | None = None,
) -> tuple[str, str]:
    """Stage 4: Export accounts as the configured JSON target.

    Uses team-scoped tokens (team_access_token) when available,
    falls back to personal tokens.
    """
    team_setting = config.get("sub2api", {}).get("require_team_tokens", "auto")
    if isinstance(team_setting, bool):
        require_team = team_setting
    else:
        require_team = bool(config.get("workspace", {}).get("re_login_enabled", False))
    if require_team:
        accounts = [
            account
            for account in accounts
            if (
                account.get("team_login_status") == "ok"
                and account.get("team_access_token")
                and account.get("team_login_error") is None
                and _is_workspace_plan(
                    account.get("team_plan_type") or account.get("plan_type")
                )
            )
            or (
                account.get("workspace_export_status") == "ok"
                and account.get("access_token")
                and account.get("workspace_chatgpt_account_id")
                and _is_workspace_plan(
                    account.get("workspace_plan_type") or account.get("plan_type")
                )
                and account.get("refresh_status") != "failed"
                and not _has_auth_invalid_join_result(account)
            )
            or (
                _has_verified_workspace_context(account)
                and not _has_auth_invalid_join_result(account)
            )
        ]
        if not accounts:
            raise RuntimeError(
                "No verified team/workspace-scoped accounts available for export"
            )

    export_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for account in accounts:
        export = dict(account)
        if account.get("team_login_status") == "ok":
            export["access_token"] = account.get("team_access_token", account.get("access_token", ""))
            export["refresh_token"] = account.get("team_refresh_token", account.get("refresh_token", ""))
            export["id_token"] = account.get("team_id_token", account.get("id_token", ""))
            export["plan_type"] = account.get("team_plan_type", account.get("plan_type", ""))
            export["chatgpt_account_id"] = account.get("team_chatgpt_account_id", account.get("chatgpt_account_id", ""))
            export["account_user_role"] = account.get("team_account_user_role", account.get("account_user_role", ""))
            export["source_type"] = "team_relogin"
        elif account.get("workspace_export_status") == "ok":
            export["plan_type"] = account.get("workspace_plan_type", account.get("plan_type", ""))
            export["chatgpt_account_id"] = account.get("workspace_chatgpt_account_id", account.get("chatgpt_account_id", ""))
            export["account_user_role"] = account.get("workspace_account_user_role", account.get("account_user_role", ""))
            export["source_type"] = "workspace_join"
        elif _has_verified_workspace_context(account):
            export["source_type"] = "workspace_check"
        # else: use registration tokens as-is
        export_pairs.append((account, export))

    export_pairs = _filter_healthy_export_accounts(config, export_pairs)
    if not export_pairs:
        raise RuntimeError("No healthy accounts available for export")

    export_accounts = [export for _, export in export_pairs]

    output_path = _resolve_export_output_path(config, output_file)
    export_format = export_format_from_config(config)

    json_str, actual_path = export_accounts_json(
        export_accounts,
        output_path,
        export_format,
    )
    state_file = workspace_state_path(config)
    for account in export_accounts:
        email = _account_email(account)
        workspace_id = str(
            account.get("workspace_chatgpt_account_id")
            or account.get("chatgpt_account_id")
            or _primary_workspace_id(config)
            or ""
        ).strip()
        if workspace_id and email and email != "?":
            set_workspace_email_state(
                state_file,
                workspace_id,
                email,
                "exported",
                mode=str(account.get("source_type") or "export"),
                extra={
                    "plan_type": account.get("workspace_plan_type")
                    or account.get("plan_type", ""),
                    "chatgpt_account_id": workspace_id,
                    "account_user_role": account.get("workspace_account_user_role")
                    or account.get("account_user_role", ""),
                    "output_file": str(actual_path),
                },
            )
    logger.info(
        f"Exported {len(export_accounts)} accounts as {export_format} to {actual_path}"
    )
    return json_str, actual_path


# ── Full pipeline ───────────────────────────────────────────────────


def run_full_pipeline(
    config: dict[str, Any],
    count: int | None = None,
    output_file: str | None = None,
    accounts_file: str | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run the complete pipeline: register → join → re-login → export.

    Args:
        config: Full config dict from config.yaml
        count: Override registration count
        output_file: Override sub2api output path
        accounts_file: Override accounts storage path

    Returns:
        Summary dict with counts
    """
    config_dir = Path(config.get("_config_dir", "."))
    af = Path(accounts_file) if accounts_file else config_dir / "registered_accounts.json"
    of = Path(output_file) if output_file else None

    logger.info("=" * 60)
    logger.info("Pipeline started: register → join → refresh/check → export")
    logger.info("=" * 60)

    # Stage 1: Register
    _raise_if_cancelled(cancel_event)
    new_accounts = run_register(config, af, count=count, cancel_event=cancel_event)
    if not new_accounts:
        logger.error("No accounts registered — pipeline aborted")
        return {
            "registered": 0,
            "joined": 0,
            "refreshed": 0,
            "exported": 0,
            "accounts_file": str(af),
            "output_file": "",
        }

    # Stage 2: Join workspace
    _raise_if_cancelled(cancel_event)
    joined_accounts = run_join_workspace(config, new_accounts, cancel_event=cancel_event)
    save_accounts(af, joined_accounts)

    re_login_enabled = config.get("workspace", {}).get("re_login_enabled", False)
    if re_login_enabled:
        # Stage 3a: Explicit team re-login. Only team-token successes are exported.
        _raise_if_cancelled(cancel_event)
        refreshed_accounts = run_re_login(
            config,
            joined_accounts,
            cancel_event=cancel_event,
        )
        save_accounts(af, refreshed_accounts)
    else:
        # Stage 3b: Default refresh/check path for personal registration tokens.
        _raise_if_cancelled(cancel_event)
        refreshed_accounts = run_refresh_tokens(
            config,
            joined_accounts,
            cancel_event=cancel_event,
        )
        save_accounts(af, refreshed_accounts)

    # Stage 4: Export (uses plan_type and account_id from check API)
    _raise_if_cancelled(cancel_event)
    all_accounts = load_accounts(af)
    if re_login_enabled:
        all_accounts = [
            account
            for account in all_accounts
            if account.get("team_login_status") == "ok"
        ]
        if not all_accounts:
            logger.error("No team-scoped tokens obtained — export aborted")
            return {
                "registered": len(new_accounts),
                "joined": sum(
                    1 for a in refreshed_accounts if a.get("join_status") == "ok"
                ),
                "refreshed": 0,
                "exported": 0,
                "accounts_file": str(af),
                "output_file": "",
            }

    try:
        json_str, actual_output = run_export(config, all_accounts, of)
        save_accounts(af, all_accounts)
    except RuntimeError as error:
        logger.error(f"Export aborted: {error}")
        return {
            "registered": len(new_accounts),
            "joined": sum(
                1 for a in refreshed_accounts if a.get("join_status") == "ok"
            ),
            "refreshed": 0,
            "exported": 0,
            "accounts_file": str(af),
            "output_file": "",
        }

    registered = len(new_accounts)
    joined = sum(1 for a in refreshed_accounts if a.get("join_status") == "ok")
    refreshed = (
        sum(1 for a in refreshed_accounts if a.get("team_login_status") == "ok")
        if re_login_enabled
        else sum(
            1 for a in refreshed_accounts
            if _is_workspace_plan(a.get("plan_type"))
            and a.get("refresh_status") != "failed"
        )
    )
    exported = _count_exported_accounts(json_str)

    logger.info("=" * 60)
    logger.info(
        f"Pipeline complete: "
        f"registered={registered}, joined={joined}, "
        f"refreshed={refreshed}, exported={exported}"
    )
    logger.info("=" * 60)

    return {
        "registered": registered,
        "joined": joined,
        "refreshed": refreshed,
        "exported": exported,
        "accounts_file": str(af),
        "output_file": actual_output,
    }
