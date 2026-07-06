"""Workspace-scoped account state tracking.

The mail provider state answers whether a mailbox credential is usable.  This
module answers a different question: whether a specific ChatGPT account/email
has already been processed for a specific workspace.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


STATE_FILENAME = "workspace_account_state.json"
IN_USE_STALE_SECONDS = 3600
BLOCKING_STATES = {
    "in_use",
    "registered",
    "logged_in",
    "joined",
    "checked",
    "exported",
    "skipped",
}

_state_lock = Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def workspace_state_path(config_or_dir: dict[str, Any] | str | Path) -> Path:
    if isinstance(config_or_dir, dict):
        configured = str(
            config_or_dir.get("workspace_state", {}).get("file") or ""
        ).strip()
        if configured:
            path = Path(configured)
            if path.is_absolute():
                return path
            return Path(config_or_dir.get("_config_dir", ".")).resolve() / path
        config_dir = Path(config_or_dir.get("_config_dir", ".")).resolve()
    else:
        config_dir = Path(config_or_dir).resolve()
    return config_dir / "data" / STATE_FILENAME


def load_workspace_state(path: str | Path) -> dict[str, Any]:
    state_path = Path(path)
    try:
        if not state_path.exists():
            return {"workspaces": {}}
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {"workspaces": {}}
    if not isinstance(data, dict):
        return {"workspaces": {}}
    workspaces = data.get("workspaces")
    if not isinstance(workspaces, dict):
        data["workspaces"] = {}
    return data


def save_workspace_state(path: str | Path, state: dict[str, Any]) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    workspaces = state.get("workspaces")
    if not isinstance(workspaces, dict):
        workspaces = {}
    ordered = {
        "workspaces": {
            ws_id: {
                email: entries[email]
                for email in sorted(entries)
                if isinstance(entries, dict)
            }
            for ws_id, entries in sorted(workspaces.items())
            if isinstance(entries, dict)
        }
    }
    state_path.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def get_workspace_email_state(
    path: str | Path,
    workspace_id: str,
    email: str,
) -> dict[str, Any] | None:
    workspace_id = str(workspace_id or "").strip()
    email_key = normalize_email(email)
    if not workspace_id or not email_key:
        return None
    state = load_workspace_state(path)
    entries = state.get("workspaces", {}).get(workspace_id, {})
    entry = entries.get(email_key) if isinstance(entries, dict) else None
    return dict(entry) if isinstance(entry, dict) else None


def _in_use_is_active(entry: dict[str, Any]) -> bool:
    if str(entry.get("state") or "") != "in_use":
        return False
    updated_at = str(entry.get("updated_at") or "")
    try:
        ts = datetime.fromisoformat(updated_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() < IN_USE_STALE_SECONDS
    except Exception:
        return False


def workspace_email_available(
    path: str | Path,
    workspace_id: str,
    email: str,
    blocking_states: set[str] | None = None,
) -> bool:
    entry = get_workspace_email_state(path, workspace_id, email)
    if not entry:
        return True
    state = str(entry.get("state") or "").strip()
    if state == "in_use":
        return not _in_use_is_active(entry)
    return state not in (blocking_states or BLOCKING_STATES)


def set_workspace_email_state(
    path: str | Path,
    workspace_id: str,
    email: str,
    state_name: str,
    *,
    mode: str = "",
    reason: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    workspace_id = str(workspace_id or "").strip()
    email_key = normalize_email(email)
    if not workspace_id or not email_key:
        return
    with _state_lock:
        state = load_workspace_state(path)
        workspaces = state.setdefault("workspaces", {})
        if not isinstance(workspaces, dict):
            workspaces = {}
            state["workspaces"] = workspaces
        entries = workspaces.setdefault(workspace_id, {})
        if not isinstance(entries, dict):
            entries = {}
            workspaces[workspace_id] = entries
        entry: dict[str, Any] = {
            "state": str(state_name or "").strip() or "unknown",
            "updated_at": now_iso(),
        }
        if mode:
            entry["mode"] = str(mode)
        if reason:
            entry["reason"] = str(reason)[:500]
        if extra:
            entry.update(extra)
        entries[email_key] = entry
        save_workspace_state(path, state)


def claim_workspace_email(
    path: str | Path,
    workspace_id: str,
    email: str,
    *,
    mode: str = "",
    extra: dict[str, Any] | None = None,
) -> bool:
    if not workspace_email_available(path, workspace_id, email):
        return False
    set_workspace_email_state(
        path,
        workspace_id,
        email,
        "in_use",
        mode=mode,
        extra=extra,
    )
    return True


def list_workspace_entries(path: str | Path, workspace_id: str = "") -> list[dict[str, Any]]:
    state = load_workspace_state(path)
    workspaces = state.get("workspaces", {})
    if not isinstance(workspaces, dict):
        return []
    selected = (
        {str(workspace_id): workspaces.get(str(workspace_id), {})}
        if workspace_id
        else workspaces
    )
    rows: list[dict[str, Any]] = []
    for ws_id, entries in selected.items():
        if not isinstance(entries, dict):
            continue
        for email, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            row = dict(entry)
            row["workspace_id"] = ws_id
            row["email"] = email
            rows.append(row)
    return rows
