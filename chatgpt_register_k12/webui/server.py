"""Standard-library local WebUI server."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import subprocess
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from chatgpt_register_k12 import __version__
from chatgpt_register_k12.config import load_config
from chatgpt_register_k12.webui.jobs import JobManager
from chatgpt_register_k12.webui.redact import redact_object
from chatgpt_register_k12.webui.terminal import run_terminal_command


logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _open_local_folder(path_value: str, base_dir: Path) -> Path:
    selected = str(path_value or "").strip()
    if not selected:
        raise ValueError("missing path")
    target = Path(selected)
    if not target.is_absolute():
        target = base_dir / target
    target = target.resolve()
    if target.is_file():
        target = target.parent
    if not target.exists() or not target.is_dir():
        raise ValueError("folder not found")
    if sys.platform.startswith("win"):
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])
    return target


class WebUIHandler(BaseHTTPRequestHandler):
    manager: JobManager
    base_dir: Path

    server_version = "chatgpt-register-k12-webui/0.1"

    def log_message(self, fmt: str, *args) -> None:
        logger.info(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            target = STATIC_DIR / path.removeprefix("/static/")
            self._serve_file(target)
            return
        if path == "/api/health":
            self._json({"ok": True, "version": __version__, "cwd": str(self.base_dir)})
            return
        if path == "/api/jobs":
            self._json({"jobs": self.manager.list_jobs()})
            return
        if path.startswith("/api/jobs/"):
            parts = [item for item in path.split("/") if item]
            if len(parts) >= 3:
                job_id = parts[2]
                if len(parts) == 4 and parts[3] == "logs":
                    query = parse_qs(parsed.query)
                    after = int((query.get("after") or ["0"])[0] or 0)
                    self._json({"logs": self.manager.logs_after(job_id, after)})
                    return
                job = self.manager.get_job(job_id)
                if not job:
                    self._json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._json({"job": job.to_dict(include_logs=False)})
                return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            body = self._read_json()
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/config/preview":
            config_path = str(body.get("config_path") or "config.yaml").strip()
            target = Path(config_path)
            if not target.is_absolute():
                target = self.base_dir / target
            try:
                config = load_config(target)
            except Exception as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"config": redact_object(config)})
            return

        if path == "/api/config/save":
            config_path = str(body.get("config_path") or "config.yaml").strip()
            target = Path(config_path)
            if not target.is_absolute():
                target = self.base_dir / target
            try:
                config = load_config(target)
                config.pop("_config_dir", None)
                if isinstance(config.get("mail"), dict):
                    config["mail"].pop("_config_dir", None)
                workspace_ids_raw = body.get("workspace_ids") or body.get("workspace_id")
                if isinstance(workspace_ids_raw, list):
                    workspace_ids = [
                        str(item).strip() for item in workspace_ids_raw if str(item).strip()
                    ]
                else:
                    workspace_ids = [
                        item.strip()
                        for item in str(workspace_ids_raw or "").splitlines()
                        if item.strip()
                    ]
                if workspace_ids:
                    config.setdefault("workspace", {})["ids"] = workspace_ids
                export_format = str(body.get("export_format") or "").strip()
                if export_format:
                    config.setdefault("export", {})["format"] = export_format
                proxy_url = str(body.get("proxy_url") or "").strip()
                config.setdefault("proxy", {})["url"] = proxy_url
                count = body.get("count")
                if count not in (None, ""):
                    config.setdefault("registration", {})["total"] = max(1, int(count))
                threads = body.get("threads")
                if threads not in (None, ""):
                    value = max(1, int(threads))
                    config.setdefault("registration", {})["threads"] = value
                    parallel = config.setdefault("parallel", {})
                    parallel["join_threads"] = value
                    parallel["refresh_threads"] = value
                    parallel["login_threads"] = value
                mailboxes = str(body.get("outlook_mailboxes") or "").strip()
                alias_enabled = bool(body.get("alias_enabled", False))
                alias_limit = max(1, int(body.get("alias_limit_per_mailbox") or 5))
                providers = config.setdefault("mail", {}).setdefault("providers", [])
                outlook = next(
                    (
                        item for item in providers
                        if isinstance(item, dict) and item.get("type") == "outlook_token"
                    ),
                    None,
                )
                if outlook is None:
                    outlook = {
                        "type": "outlook_token",
                        "enable": True,
                        "label": "Outlook Pool",
                        "mode": "auto",
                    }
                    providers.insert(0, outlook)
                outlook["enable"] = True
                outlook["mode"] = str(outlook.get("mode") or "auto")
                outlook["alias_enabled"] = alias_enabled
                outlook["alias_limit_per_mailbox"] = alias_limit
                if mailboxes:
                    outlook["mailboxes"] = mailboxes
                config["mail"]["alias_enabled"] = alias_enabled
                config["mail"]["alias_limit_per_mailbox"] = alias_limit
                config.setdefault("web", {})["host"] = str(body.get("host") or "127.0.0.1")
                config.setdefault("web", {})["port"] = int(body.get("port") or 8787)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    yaml.safe_dump(
                        config,
                        allow_unicode=True,
                        sort_keys=False,
                        default_flow_style=False,
                    ),
                    encoding="utf-8",
                )
            except Exception as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True, "config": redact_object(config)})
            return

        if path == "/api/jobs":
            try:
                job = self.manager.create_job(
                    str(body.get("action") or ""),
                    body,
                )
            except Exception as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"job": job.to_dict()}, HTTPStatus.CREATED)
            return

        if path == "/api/terminal/run":
            command = str(body.get("command") or "")
            timeout = int(body.get("timeout") or 120)
            result = run_terminal_command(command, self.base_dir, timeout=timeout)
            self._json({"result": result}, HTTPStatus.OK)
            return

        if path == "/api/open-folder":
            try:
                opened = _open_local_folder(str(body.get("path") or ""), self.base_dir)
            except Exception as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True, "path": str(opened)}, HTTPStatus.OK)
            return

        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            parts = [item for item in path.split("/") if item]
            if len(parts) == 4:
                ok = self.manager.cancel(parts[2])
                self._json({"ok": ok}, HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND)
                return

        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _read_json(self) -> dict:
        length = int(self.headers.get("content-length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as error:
            raise ValueError("invalid JSON body") from error
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _json(self, data: dict, status: int = HTTPStatus.OK) -> None:
        payload = json.dumps(redact_object(data), ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_file(self, path: Path, content_type: str = "") -> None:
        try:
            resolved = path.resolve()
            if not str(resolved).startswith(str(STATIC_DIR.resolve())):
                raise FileNotFoundError
            data = resolved.read_bytes()
        except Exception:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        mime = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", mime)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_server(
    config_path: str | Path = "config.yaml",
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = False,
) -> None:
    base_dir = Path(config_path).resolve().parent
    manager = JobManager(base_dir)

    class Handler(WebUIHandler):
        pass

    Handler.manager = manager
    Handler.base_dir = base_dir

    server = ThreadingHTTPServer((host, int(port)), Handler)
    url = f"http://{host}:{int(port)}/"
    logger.info("WebUI listening on %s", url)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
