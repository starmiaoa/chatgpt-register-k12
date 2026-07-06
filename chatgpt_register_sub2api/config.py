"""Configuration loader for chatgpt-register-sub2api.

Loads config.yaml, validates, and merges with CLI overrides.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_FILE = Path("config.yaml")

DEFAULT_CONFIG: dict[str, Any] = {
    "mail": {
        "providers": [
            {
                "type": "outlook_token",
                "enable": True,
                "label": "Outlook Pool",
                "mode": "auto",
                "alias_enabled": True,
                "alias_limit_per_mailbox": 5,
                "mailboxes": "",
            },
            {
                "type": "gmail_oauth",
                "enable": False,
                "label": "Gmail OAuth Pool",
                "imap_host": "imap.gmail.com",
                "message_limit": 10,
                "mailboxes": "",
            },
        ],
        "request_timeout": 30,
        "wait_timeout": 30,
        "wait_interval": 2,
        "alias_enabled": True,
        "alias_limit_per_mailbox": 5,
    },
    "proxy": {
        "url": "",
        "flaresolverr_url": "",
    },
    "registration": {
        "threads": 3,
        "total": 10,
    },
    "parallel": {
        "join_threads": 3,
        "refresh_threads": 3,
        "login_threads": 1,
    },
    "workspace": {
        "enabled": True,
        "ids": [],
        "route": "k12_request",
        "re_login_enabled": False,
        "export_plan_type": "k12",
        "max_retries": 3,
        "retry_backoff_ms": 5000,
    },
    "workspace_state": {
        "file": "data/workspace_account_state.json",
    },
    "existing_login": {
        "mode": "email_otp",
        "fallback_to_password": False,
    },
    "export": {
        "format": "sub2api",
        "output_file": "",
    },
    "sub2api": {
        "enabled": True,
        "output_file": "sub2api_bundle.json",
        "require_team_tokens": "auto",
        "health_check": True,
        "health_check_endpoint": "models",
        "health_check_timeout": 30,
        "health_check_retries": 2,
        "health_check_backoff_ms": 3000,
        "health_check_delay_seconds": 5,
    },
    "output": {
        "archive_runs": True,
        "runs_dir": "runs",
    },
    "logging": {
        "level": "INFO",
        "file": "",
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8787,
    },
}


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate config from a YAML file.

    Merges with defaults so missing keys get sensible values.
    """
    config_file = Path(path) if path else DEFAULT_CONFIG_FILE
    config = deepcopy(DEFAULT_CONFIG)

    if config_file.exists():
        try:
            raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _deep_merge(config, raw)
        except Exception as e:
            raise ValueError(f"Failed to parse {config_file}: {e}") from e

    # Resolve relative paths from config file's directory
    config["_config_dir"] = str(config_file.parent.resolve())
    if isinstance(config.get("mail"), dict):
        config["mail"]["_config_dir"] = config["_config_dir"]

    return config


def _deep_merge(base: dict, overlay: dict) -> None:
    """Merge overlay into base in-place, recursively."""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def generate_default_config(path: str | Path = DEFAULT_CONFIG_FILE) -> Path:
    """Write the default config.yaml to disk.

    Returns the path written.
    """
    output = Path(path).resolve()
    if output.exists():
        raise FileExistsError(f"{output} already exists — not overwriting")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        yaml.dump(DEFAULT_CONFIG, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return output


def get_output_dir(config: dict[str, Any], cli_output_dir: str = "") -> Path:
    """Determine the output directory for data files.

    Priority: CLI arg > config _config_dir > cwd
    """
    if cli_output_dir:
        return Path(cli_output_dir).resolve()
    config_dir = config.get("_config_dir", "")
    if config_dir:
        return Path(config_dir)
    return Path.cwd()
