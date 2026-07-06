"""Background job manager for the local WebUI."""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chatgpt_register_k12.config import load_config
from chatgpt_register_k12.export.formats import (
    count_exported_json,
    output_filename_from_config,
)
from chatgpt_register_k12.pipeline import (
    PipelineCancelled,
    create_run_output_dir,
    load_accounts,
    preview_mailbox_candidates,
    run_export,
    run_full_pipeline,
    run_join_workspace,
    run_login_existing,
    run_refresh_tokens,
    run_register,
    save_accounts,
    workspace_state_entries,
)
from chatgpt_register_k12.webui.redact import redact_object, redact_text


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _count_exported(json_str: str) -> int:
    return count_exported_json(json_str)


@dataclass
class Job:
    id: str
    action: str
    params: dict[str, Any]
    status: str = "queued"
    stage: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str = ""
    finished_at: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    error: str = ""
    cancel_requested: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    logs: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=2000))
    _seq: int = 0

    def add_log(self, level: str, message: str) -> None:
        self._seq += 1
        self.logs.append(
            {
                "seq": self._seq,
                "time": _now(),
                "level": str(level or "INFO"),
                "message": redact_text(message),
            }
        )

    def to_dict(self, include_logs: bool = False) -> dict[str, Any]:
        data = {
            "id": self.id,
            "action": self.action,
            "status": self.status,
            "stage": self.stage,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": redact_object(self.summary),
            "artifacts": redact_object(self.artifacts),
            "error": redact_text(self.error),
            "cancel_requested": self.cancel_requested,
        }
        if include_logs:
            data["logs"] = list(self.logs)
        return data


class JobLogHandler(logging.Handler):
    def __init__(self, job: Job):
        super().__init__()
        self.job = job

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        self.job.add_log(record.levelname, message)


class JobManager:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir).resolve()
        self.jobs: dict[str, Job] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run_loop, daemon=True)
        self._worker.start()

    def create_job(self, action: str, params: dict[str, Any]) -> Job:
        action = str(action or "").strip()
        if action not in {
            "run",
            "existing-token",
            "register",
            "login-existing",
            "join-workspace",
            "refresh",
            "export",
            "preview",
        }:
            raise ValueError(f"Unsupported action: {action}")
        job = Job(id=uuid.uuid4().hex[:12], action=action, params=dict(params or {}))
        with self._lock:
            self.jobs[job.id] = job
        self._queue.put(job.id)
        return job

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.to_dict() for job in self.jobs.values()]

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            return self.jobs.get(job_id)

    def logs_after(self, job_id: str, after: int = 0) -> list[dict[str, Any]]:
        job = self.get_job(job_id)
        if not job:
            return []
        return [item for item in job.logs if int(item.get("seq", 0)) > after]

    def cancel(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        if not job:
            return False
        if job.status in {"succeeded", "failed", "cancelled"}:
            return False
        job.cancel_requested = True
        job.cancel_event.set()
        job.add_log("WARNING", "Cancellation requested")
        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = _now()
        return True

    @staticmethod
    def _raise_if_cancelled(job: Job) -> None:
        if job.cancel_requested or job.cancel_event.is_set():
            raise PipelineCancelled("Task cancelled by user")

    def _run_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.get_job(job_id)
            if not job:
                continue
            if job.status == "cancelled":
                continue
            self._run_job(job)

    def _load_config_for_job(self, job: Job) -> dict[str, Any]:
        config_path = str(job.params.get("config_path") or "config.yaml").strip()
        path = Path(config_path)
        if not path.is_absolute():
            path = self.base_dir / path
        config = load_config(path)

        workspace_ids = job.params.get("workspace_ids") or job.params.get("workspace_id")
        if workspace_ids:
            if not isinstance(workspace_ids, list):
                workspace_ids = [workspace_ids]
            config.setdefault("workspace", {})["ids"] = [
                str(item).strip() for item in workspace_ids if str(item).strip()
            ]

        threads = job.params.get("threads")
        if threads is not None:
            value = max(1, int(threads))
            config.setdefault("registration", {})["threads"] = value
            parallel = config.setdefault("parallel", {})
            parallel["join_threads"] = value
            parallel["refresh_threads"] = value
            parallel["login_threads"] = value

        export_format = str(job.params.get("export_format") or "").strip()
        if export_format:
            config.setdefault("export", {})["format"] = export_format

        return config

    def _path_param(self, config: dict[str, Any], value: Any, default: str) -> Path:
        selected = str(value or default).strip() or default
        path = Path(selected)
        if path.is_absolute():
            return path
        return Path(config.get("_config_dir", self.base_dir)) / path

    def _prepare_run_files(
        self,
        config: dict[str, Any],
        count: int | None,
        accounts_value: Any = "",
        output_value: Any = "",
    ) -> tuple[Path, Path, Path]:
        run_dir = create_run_output_dir(config, count)
        accounts_file = Path(str(accounts_value)) if accounts_value else run_dir / "registered_accounts.json"
        if not accounts_file.is_absolute():
            accounts_file = run_dir / accounts_file
        output_default = output_filename_from_config(config)
        output_file = Path(str(output_value)) if output_value else run_dir / output_default
        if not output_file.is_absolute():
            output_file = run_dir / output_file
        self._set_job_log_file(config, run_dir / "test_run.log")
        return run_dir, accounts_file, output_file

    @staticmethod
    def _set_job_log_file(config: dict[str, Any], log_path: Path) -> None:
        config.setdefault("logging", {})["file"] = str(log_path)

    def _run_job(self, job: Job) -> None:
        job.status = "running"
        job.started_at = _now()
        handler = JobLogHandler(job)
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)-5s] %(message)s",
            datefmt="%H:%M:%S",
        )
        handler.setFormatter(formatter)
        root = logging.getLogger()
        root.addHandler(handler)
        previous_level = root.level
        root.setLevel(logging.INFO)
        file_handler: logging.Handler | None = None

        try:
            config = self._load_config_for_job(job)
            count = job.params.get("count")
            count_value = int(count) if count not in (None, "") else None
            self._raise_if_cancelled(job)

            if job.action == "preview":
                job.stage = "preview"
                candidates = preview_mailbox_candidates(config, count=count_value)
                job.summary = {
                    "candidates": candidates,
                    "workspace_state": workspace_state_entries(config),
                }
                return

            if job.action in {"run", "existing-token", "register", "login-existing"}:
                run_dir, accounts_file, output_file = self._prepare_run_files(
                    config,
                    count_value,
                    job.params.get("accounts_file") or job.params.get("accounts"),
                    job.params.get("output_file") or job.params.get("output"),
                )
                job.artifacts.update(
                    {
                        "run_dir": str(run_dir),
                        "accounts_file": str(accounts_file),
                        "output_file": str(output_file),
                    }
                )
            else:
                accounts_file = self._path_param(
                    config,
                    job.params.get("input_file") or job.params.get("accounts_file"),
                    "registered_accounts.json",
                )
                output_file = self._path_param(
                    config,
                    job.params.get("output_file") or job.params.get("output"),
                    output_filename_from_config(config),
                )
                job.artifacts.update(
                    {
                        "accounts_file": str(accounts_file),
                        "output_file": str(output_file),
                    }
                )

            log_file = str(config.get("logging", {}).get("file") or "").strip()
            if log_file:
                file_path = Path(log_file)
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_handler = logging.FileHandler(file_path, encoding="utf-8")
                file_handler.setFormatter(formatter)
                root.addHandler(file_handler)

            if job.action == "run":
                job.stage = "run"
                summary = run_full_pipeline(
                    config,
                    count=count_value,
                    output_file=str(output_file),
                    accounts_file=str(accounts_file),
                    cancel_event=job.cancel_event,
                )
                job.summary = summary
                job.artifacts["accounts_file"] = str(summary.get("accounts_file") or accounts_file)
                if summary.get("output_file"):
                    job.artifacts["output_file"] = str(summary["output_file"])
            elif job.action == "existing-token":
                job.stage = "login-existing"
                self._raise_if_cancelled(job)
                logged_accounts = run_login_existing(
                    config,
                    accounts_file,
                    count=count_value,
                    cancel_event=job.cancel_event,
                )
                if not logged_accounts:
                    raise RuntimeError("No existing accounts logged in")

                job.stage = "join-workspace"
                self._raise_if_cancelled(job)
                joined_accounts = run_join_workspace(
                    config,
                    logged_accounts,
                    cancel_event=job.cancel_event,
                )
                save_accounts(accounts_file, joined_accounts)

                job.stage = "refresh"
                self._raise_if_cancelled(job)
                refreshed_accounts = run_refresh_tokens(
                    config,
                    joined_accounts,
                    cancel_event=job.cancel_event,
                )
                save_accounts(accounts_file, refreshed_accounts)

                job.stage = "export"
                self._raise_if_cancelled(job)
                json_str, actual_path = run_export(config, refreshed_accounts, output_file)
                save_accounts(accounts_file, refreshed_accounts)
                exported = _count_exported(json_str)
                job.artifacts["output_file"] = str(actual_path)
                job.summary = {
                    "logged_in": len(logged_accounts),
                    "joined": sum(1 for a in joined_accounts if a.get("join_status") == "ok"),
                    "refreshed": sum(1 for a in refreshed_accounts if a.get("refresh_status") == "ok"),
                    "exported": exported,
                    "accounts_file": str(accounts_file),
                    "output_file": str(actual_path),
                }
            elif job.action == "register":
                job.stage = "register"
                accounts = run_register(
                    config,
                    accounts_file,
                    count=count_value,
                    cancel_event=job.cancel_event,
                )
                job.summary = {"registered": len(accounts)}
            elif job.action == "login-existing":
                job.stage = "login-existing"
                accounts = run_login_existing(
                    config,
                    accounts_file,
                    count=count_value,
                    cancel_event=job.cancel_event,
                )
                job.summary = {"logged_in": len(accounts)}
            elif job.action == "join-workspace":
                job.stage = "join-workspace"
                accounts = run_join_workspace(
                    config,
                    load_accounts(accounts_file),
                    cancel_event=job.cancel_event,
                )
                save_accounts(accounts_file, accounts)
                job.summary = {"joined": sum(1 for a in accounts if a.get("join_status") == "ok")}
            elif job.action == "refresh":
                job.stage = "refresh"
                accounts = run_refresh_tokens(
                    config,
                    load_accounts(accounts_file),
                    cancel_event=job.cancel_event,
                )
                save_accounts(accounts_file, accounts)
                job.summary = {"refreshed": sum(1 for a in accounts if a.get("refresh_status") == "ok")}
            elif job.action == "export":
                job.stage = "export"
                accounts = load_accounts(accounts_file)
                json_str, actual_path = run_export(
                    config,
                    accounts,
                    output_file,
                )
                save_accounts(accounts_file, accounts)
                job.artifacts["output_file"] = str(actual_path)
                job.summary = {"exported": _count_exported(json_str)}

            job.status = "succeeded"
        except PipelineCancelled as error:
            job.status = "cancelled"
            job.error = str(error)
            job.add_log("WARNING", str(error))
        except Exception as error:
            job.status = "failed"
            job.error = str(error)
            job.add_log("ERROR", str(error))
        finally:
            if file_handler:
                root.removeHandler(file_handler)
                file_handler.close()
            root.removeHandler(handler)
            root.setLevel(previous_level)
            job.finished_at = _now()
            if job.status == "running":
                job.status = "succeeded"
            job.stage = "done" if job.status == "succeeded" else job.stage
