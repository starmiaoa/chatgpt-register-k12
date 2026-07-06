"""CLI entry point for chatgpt-register-sub2api.

Subcommands:
  init             Write default config.yaml
  register         Register N ChatGPT accounts
  join-workspace   Join registered accounts to K12 workspace
  refresh          Refresh/check existing accounts
  login-team       Re-login with Team space selection
  export           Export accounts JSON
  run              Full pipeline (register → join → login → export)
  web              Start local WebUI
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from chatgpt_register_sub2api import __version__
from chatgpt_register_sub2api.config import (
    DEFAULT_CONFIG_FILE,
    generate_default_config,
    load_config,
)
from chatgpt_register_sub2api.export.formats import (
    output_filename_from_config,
    supported_export_formats,
)
from chatgpt_register_sub2api.pipeline import (
    create_run_output_dir,
    load_accounts,
    run_export,
    run_full_pipeline,
    run_join_workspace,
    run_refresh_tokens,
    run_re_login,
    run_register,
    save_accounts,
)
from chatgpt_register_sub2api.webui.server import run_server


def setup_logging(config: dict, verbose: bool = False) -> None:
    """Configure Python logging based on config + CLI verbosity."""
    log_cfg = config.get("logging", {})
    level_name = "DEBUG" if verbose else str(log_cfg.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = "%(asctime)s [%(levelname)-5s] %(message)s"
    datefmt = "%H:%M:%S"

    log_file = str(log_cfg.get("file", "")).strip()
    if log_file:
        log_path = Path(log_file)
        if not log_path.is_absolute():
            log_path = Path(config.get("_config_dir", ".")) / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=level,
            format=fmt,
            datefmt=datefmt,
            handlers=[
                logging.FileHandler(log_path, encoding="utf-8"),
                logging.StreamHandler(sys.stderr),
            ],
        )
    else:
        logging.basicConfig(
            level=level,
            format=fmt,
            datefmt=datefmt,
            stream=sys.stderr,
        )


def _bool_config_value(value, default: bool = False) -> bool:
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


def _path_under_run_dir(run_dir: Path, value: str | None, default_name: str) -> Path:
    selected = str(value or default_name).strip() or default_name
    path = Path(selected)
    if path.is_absolute():
        return path
    return run_dir / path


def _prepare_run_archive(config: dict, args) -> tuple[Path | None, Path | None, Path | None]:
    output_cfg = config.get("output", {})
    if not isinstance(output_cfg, dict):
        output_cfg = {}
    if not _bool_config_value(output_cfg.get("archive_runs"), True):
        return None, Path(args.accounts) if args.accounts else None, Path(args.output) if args.output else None

    run_dir = create_run_output_dir(config, args.count)
    default_output = output_filename_from_config(config)
    accounts_file = _path_under_run_dir(
        run_dir,
        args.accounts,
        "registered_accounts.json",
    )
    output_file = _path_under_run_dir(run_dir, args.output, default_output)

    log_cfg = config.setdefault("logging", {})
    log_file = str(log_cfg.get("file") or "").strip()
    if log_file:
        log_cfg["file"] = str(_path_under_run_dir(run_dir, log_file, "test_run.log"))

    return run_dir, accounts_file, output_file


def _apply_threads_override(config: dict, threads: int | None, *stages: str) -> None:
    """Apply a CLI thread-count override to selected pipeline stages."""
    if threads is None:
        return
    value = max(1, int(threads))
    if "register" in stages:
        config.setdefault("registration", {})["threads"] = value
    parallel = config.setdefault("parallel", {})
    if "join" in stages:
        parallel["join_threads"] = value
    if "refresh" in stages:
        parallel["refresh_threads"] = value
    if "login" in stages:
        parallel["login_threads"] = value


def cmd_init(args) -> int:
    """Write default config.yaml."""
    path = Path(args.config) if args.config else DEFAULT_CONFIG_FILE
    try:
        output = generate_default_config(path)
        print(f"Config written to {output}")
        return 0
    except FileExistsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_register(args) -> int:
    """Register N ChatGPT accounts."""
    config = load_config(args.config)
    _apply_threads_override(config, args.threads, "register")
    setup_logging(config, args.verbose)
    logger = logging.getLogger(__name__)

    config_dir = Path(config.get("_config_dir", "."))
    accounts_file = config_dir / "registered_accounts.json"

    results = run_register(
        config=config,
        accounts_file=accounts_file,
        count=args.count,
    )

    print(f"\nRegistered: {len(results)} accounts")
    for acc in results:
        print(f"  {acc['email']}")
    return 0 if results else 1


def cmd_join_workspace(args) -> int:
    """Join registered accounts to K12 workspace."""
    config = load_config(args.config)
    _apply_threads_override(config, args.threads, "join")
    setup_logging(config, args.verbose)
    logger = logging.getLogger(__name__)

    # Override workspace IDs from CLI
    if args.workspace_id:
        config.setdefault("workspace", {})["ids"] = args.workspace_id

    config_dir = Path(config.get("_config_dir", "."))
    input_file = Path(args.input) if args.input else config_dir / "registered_accounts.json"
    accounts = load_accounts(input_file)

    if not accounts:
        print(f"No accounts found in {input_file}", file=sys.stderr)
        return 1

    accounts = run_join_workspace(config, accounts)
    save_accounts(input_file, accounts)

    joined = sum(1 for a in accounts if a.get("join_status") == "ok")
    print(f"\nJoined: {joined}/{len(accounts)} accounts")
    return 0 if joined == len(accounts) else 1


def cmd_login_team(args) -> int:
    """Re-login with Team space selection."""
    config = load_config(args.config)
    _apply_threads_override(config, args.threads, "login")
    config.setdefault("workspace", {})["re_login_enabled"] = True
    if args.workspace_id:
        config.setdefault("workspace", {})["ids"] = args.workspace_id
    setup_logging(config, args.verbose)
    logger = logging.getLogger(__name__)

    config_dir = Path(config.get("_config_dir", "."))
    input_file = Path(args.input) if args.input else config_dir / "registered_accounts.json"
    accounts = load_accounts(input_file)

    if not accounts:
        print(f"No accounts found in {input_file}", file=sys.stderr)
        return 1
    if not config.get("workspace", {}).get("ids"):
        print("No workspace ID configured for team login", file=sys.stderr)
        return 1

    accounts = run_re_login(config, accounts)
    save_accounts(input_file, accounts)

    team_logged = sum(1 for a in accounts if a.get("team_login_status") == "ok")
    print(f"\nTeam logged: {team_logged}/{len(accounts)} accounts")
    return 0 if team_logged == len(accounts) else 1


def cmd_refresh(args) -> int:
    """Refresh tokens and check account context for existing accounts."""
    config = load_config(args.config)
    _apply_threads_override(config, args.threads, "refresh")
    if args.workspace_id:
        config.setdefault("workspace", {})["ids"] = args.workspace_id
    setup_logging(config, args.verbose)

    config_dir = Path(config.get("_config_dir", "."))
    input_file = Path(args.input) if args.input else config_dir / "registered_accounts.json"
    accounts = load_accounts(input_file)

    if not accounts:
        print(f"No accounts found in {input_file}", file=sys.stderr)
        return 1

    accounts = run_refresh_tokens(config, accounts)
    save_accounts(input_file, accounts)

    checked = sum(1 for a in accounts if a.get("refresh_status") == "ok")
    print(f"\nRefreshed/checked: {checked}/{len(accounts)} accounts")
    return 0 if checked else 1


def cmd_export(args) -> int:
    """Export to sub2api JSON."""
    config = load_config(args.config)
    if args.format:
        config.setdefault("export", {})["format"] = args.format
    setup_logging(config, args.verbose)
    logger = logging.getLogger(__name__)

    config_dir = Path(config.get("_config_dir", "."))
    input_file = Path(args.input) if args.input else config_dir / "registered_accounts.json"
    accounts = load_accounts(input_file)

    if not accounts:
        print(f"No accounts found in {input_file}", file=sys.stderr)
        return 1

    output_file = Path(args.output) if args.output else None
    try:
        json_str, actual_path = run_export(config, accounts, output_file)
    except RuntimeError as error:
        print(f"Export failed: {error}", file=sys.stderr)
        return 1
    save_accounts(input_file, accounts)

    if args.stdout:
        print(json_str)
    else:
        print(f"Exported to {actual_path}")

    return 0


def cmd_run(args) -> int:
    """Run the full pipeline."""
    config = load_config(args.config)
    if args.format:
        config.setdefault("export", {})["format"] = args.format
    _apply_threads_override(
        config,
        args.threads,
        "register",
        "join",
        "refresh",
        "login",
    )
    run_dir, accounts_file, output_file = _prepare_run_archive(config, args)
    setup_logging(config, args.verbose)

    # Override workspace IDs from CLI
    if args.workspace_id:
        config.setdefault("workspace", {})["ids"] = args.workspace_id

    summary = run_full_pipeline(
        config=config,
        count=args.count,
        output_file=str(output_file) if output_file else None,
        accounts_file=str(accounts_file) if accounts_file else None,
    )
    if run_dir:
        summary["run_dir"] = str(run_dir)

    print(f"\n{'='*40}")
    print(f"Pipeline Summary:")
    print(f"  Registered:  {summary['registered']}")
    print(f"  Joined:      {summary['joined']}")
    print(f"  K12 Refreshed: {summary['refreshed']}")
    print(f"  Exported:    {summary['exported']}")
    if summary.get("run_dir"):
        print(f"  Run Dir:     {summary['run_dir']}")
    print(f"  Accounts:    {summary['accounts_file']}")
    if summary.get("output_file"):
        print(f"  Output:      {summary['output_file']}")

    return 0 if summary["exported"] > 0 else 1


def cmd_web(args) -> int:
    config = load_config(args.config)
    setup_logging(config, verbose=args.verbose)
    web_cfg = config.get("web", {})
    host = args.host or str(web_cfg.get("host") or "127.0.0.1")
    port = args.port or int(web_cfg.get("port") or 8787)
    run_server(
        config_path=args.config or DEFAULT_CONFIG_FILE,
        host=host,
        port=port,
        open_browser=args.open,
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="chatgpt-register",
        description="ChatGPT 账号注册 + K12 母号加入 + 多格式 JSON 导出",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ── init ──
    p_init = sub.add_parser("init", help="Write default config.yaml")
    p_init.add_argument("--config", "-c", default=None, help="Config file path")
    p_init.set_defaults(func=cmd_init)

    # ── register ──
    p_reg = sub.add_parser("register", help="Register ChatGPT accounts")
    p_reg.add_argument("--config", "-c", default=None, help="Config file path")
    p_reg.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    p_reg.add_argument("--count", "-n", type=int, default=None, help="Number of accounts")
    p_reg.add_argument("--threads", "-t", type=int, default=None, help="Registration workers")
    p_reg.set_defaults(func=cmd_register)

    # ── join-workspace ──
    p_join = sub.add_parser("join-workspace", help="Join workspace")
    p_join.add_argument("--config", "-c", default=None, help="Config file path")
    p_join.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    p_join.add_argument("--workspace-id", action="append", default=None, help="Workspace UUID (repeatable)")
    p_join.add_argument("--input", "-i", default=None, help="Input accounts JSON")
    p_join.add_argument("--threads", "-t", type=int, default=None, help="Workspace join workers")
    p_join.set_defaults(func=cmd_join_workspace)

    # ── login-team ──
    p_login = sub.add_parser("login-team", help="Re-login with Team space")
    p_login.add_argument("--config", "-c", default=None, help="Config file path")
    p_login.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    p_login.add_argument("--input", "-i", default=None, help="Input accounts JSON")
    p_login.add_argument("--workspace-id", action="append", default=None, help="Workspace UUID (repeatable)")
    p_login.add_argument("--threads", "-t", type=int, default=None, help="Team login workers")
    p_login.set_defaults(func=cmd_login_team)

    # ── refresh ──
    p_refresh = sub.add_parser("refresh", help="Refresh/check existing accounts")
    p_refresh.add_argument("--config", "-c", default=None, help="Config file path")
    p_refresh.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    p_refresh.add_argument("--input", "-i", default=None, help="Input accounts JSON")
    p_refresh.add_argument("--workspace-id", action="append", default=None, help="Workspace UUID (repeatable)")
    p_refresh.add_argument("--threads", "-t", type=int, default=None, help="Refresh/check workers")
    p_refresh.set_defaults(func=cmd_refresh)

    # ── export ──
    p_export = sub.add_parser("export", help="Export accounts JSON")
    p_export.add_argument("--config", "-c", default=None, help="Config file path")
    p_export.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    p_export.add_argument("--output", "-o", default=None, help="Output file path")
    p_export.add_argument("--input", "-i", default=None, help="Input accounts JSON")
    p_export.add_argument(
        "--format",
        choices=supported_export_formats(),
        default=None,
        help="Export format (default: config export.format, usually sub2api)",
    )
    p_export.add_argument("--stdout", action="store_true", help="Print to stdout")
    p_export.set_defaults(func=cmd_export)

    # ── run ──
    p_run = sub.add_parser("run", help="Full pipeline")
    p_run.add_argument("--config", "-c", default=None, help="Config file path")
    p_run.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    p_run.add_argument("--count", "-n", type=int, default=None, help="Number of accounts")
    p_run.add_argument("--workspace-id", action="append", default=None, help="Workspace UUID (repeatable)")
    p_run.add_argument("--output", "-o", default=None, help="Output JSON file")
    p_run.add_argument(
        "--format",
        choices=supported_export_formats(),
        default=None,
        help="Export format (default: config export.format, usually sub2api)",
    )
    p_run.add_argument("--accounts", default=None, help="Accounts store JSON file")
    p_run.add_argument("--threads", "-t", type=int, default=None, help="Workers per pipeline stage")
    p_run.set_defaults(func=cmd_run)

    # ── web ──
    p_web = sub.add_parser("web", help="Start local WebUI")
    p_web.add_argument("--config", "-c", default=None, help="Config file path")
    p_web.add_argument("--host", default=None, help="Bind host")
    p_web.add_argument("--port", type=int, default=None, help="Bind port")
    p_web.add_argument("--open", action="store_true", help="Open browser")
    p_web.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    p_web.set_defaults(func=cmd_web)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        sys.exit(1)

    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
