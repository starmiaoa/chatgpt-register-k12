from __future__ import annotations

import json
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.request import Request, urlopen

from chatgpt_register_sub2api.login.login_flow import (
    PasswordRequiredError,
    _complete_login_flow,
)
from chatgpt_register_sub2api.pipeline import run_export
from chatgpt_register_sub2api.webui.terminal import run_terminal_command
from chatgpt_register_sub2api.webui.redact import redact_object, redact_text
from chatgpt_register_sub2api.webui.jobs import JobManager
from chatgpt_register_sub2api.webui.server import WebUIHandler
from chatgpt_register_sub2api.workspace_state import (
    get_workspace_email_state,
    set_workspace_email_state,
    workspace_email_available,
)


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self.headers = {}
        self.history = []
        self.url = "https://chatgpt.com/backend-api/models"

    def json(self):
        if self._json_data is None:
            raise ValueError("not json")
        return self._json_data


class RefreshThenHealthySession:
    def __init__(self):
        self.get_calls = 0
        self.post_calls = 0

    def get(self, *args, **kwargs):
        self.get_calls += 1
        if self.get_calls == 1:
            return FakeResponse(
                401,
                '{"error":{"code":"token_invalidated","message":"invalidated"}}',
            )
        return FakeResponse(200, "{}", {})

    def post(self, *args, **kwargs):
        self.post_calls += 1
        return FakeResponse(
            200,
            "{}",
            {
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "id_token": "new-id-token",
            },
        )

    def close(self):
        pass


class WorkspaceStateTests(unittest.TestCase):
    def test_same_workspace_email_is_not_available_after_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace_account_state.json"
            set_workspace_email_state(path, "ws-a", "User@Outlook.com", "exported")

            self.assertFalse(workspace_email_available(path, "ws-a", "user@outlook.com"))
            entry = get_workspace_email_state(path, "ws-a", " user@outlook.com ")
            self.assertEqual(entry["state"], "exported")

    def test_same_email_different_workspace_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace_account_state.json"
            set_workspace_email_state(path, "ws-a", "user@outlook.com", "exported")

            self.assertTrue(workspace_email_available(path, "ws-b", "user@outlook.com"))


class LoginOtpTests(unittest.TestCase):
    def test_otp_only_login_reports_password_required(self):
        with self.assertRaises(PasswordRequiredError):
            _complete_login_flow(
                session=object(),
                device_id="device",
                email="user@example.com",
                mail_config={},
                workspace_id="",
                response_data={"page": {"type": "login_password"}},
                code_verifier="verifier",
                password=None,
            )


class ExportHealthFallbackTests(unittest.TestCase):
    def test_export_health_refresh_fallback_recovers_invalid_access_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "sub2api_bundle.json"
            state = Path(tmp) / "workspace_account_state.json"
            account = {
                "email": "user@example.com",
                "access_token": "old-access-token",
                "refresh_token": "old-refresh-token",
                "id_token": "old-id-token",
                "plan_type": "k12",
                "chatgpt_account_id": "ws-a",
                "account_user_role": "standard-user",
                "refresh_status": "ok",
            }
            session = RefreshThenHealthySession()
            config = {
                "_config_dir": tmp,
                "proxy": {"url": ""},
                "workspace": {"ids": ["ws-a"]},
                "workspace_state": {"file": str(state)},
                "existing_login": {"mode": "disabled"},
                "sub2api": {
                    "health_check": True,
                    "health_check_endpoint": "models",
                    "health_check_retries": 1,
                    "health_check_delay_seconds": 0,
                },
            }

            with patch(
                "chatgpt_register_sub2api.pipeline._create_http_session",
                return_value=session,
            ):
                json_str, actual_path = run_export(config, [account], out)

            self.assertTrue(Path(actual_path).exists())
            self.assertEqual(Path(actual_path).name, out.name)
            self.assertEqual(account["access_token"], "new-access-token")
            self.assertEqual(account["refresh_token"], "new-refresh-token")
            self.assertEqual(session.post_calls, 1)
            data = json.loads(json_str)
            self.assertEqual(len(data["accounts"]), 1)
            self.assertEqual(
                get_workspace_email_state(state, "ws-a", "user@example.com")["state"],
                "exported",
            )


class RedactionTests(unittest.TestCase):
    def test_redact_object_masks_tokens_and_passwords(self):
        payload = {
            "email": "user@example.com",
            "password": "super-secret",
            "access_token": "access-token-secret",
            "nested": {"refresh_token": "refresh-token-secret"},
        }
        redacted = redact_object(payload)
        text = json.dumps(redacted)

        self.assertNotIn("super-secret", text)
        self.assertNotIn("access-token-secret", text)
        self.assertNotIn("refresh-token-secret", text)
        self.assertIn("u***@example.com", text)

    def test_redact_text_masks_mailbox_pool_lines(self):
        text = redact_text(
            "user@example.com----password----client----refresh Bearer abcdefghijklmnop"
        )

        self.assertNotIn("password", text)
        self.assertNotIn("abcdefghijklmnop", text)
        self.assertIn("Bearer ***", text)

    def test_terminal_command_output_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_terminal_command(
                'python -c "print(\'user@example.com----pw----cid----rt Bearer abcdefghijklmnop\')"',
                tmp,
            )

        self.assertTrue(result["ok"])
        self.assertNotIn("pw----cid----rt", result["output"])
        self.assertNotIn("abcdefghijklmnop", result["output"])
        self.assertIn("Bearer ***", result["output"])


class WebUIServerTests(unittest.TestCase):
    def test_config_save_accepts_multiple_workspace_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"

            class Handler(WebUIHandler):
                pass

            Handler.base_dir = Path(tmp)
            Handler.manager = JobManager(Path(tmp))
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps(
                    {
                        "config_path": str(config_path),
                        "workspace_ids": ["ws-a", "ws-b"],
                        "export_format": "9router",
                        "alias_enabled": True,
                        "alias_limit_per_mailbox": 5,
                    }
                ).encode("utf-8")
                req = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/api/config/save",
                    data=body,
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                payload = json.loads(urlopen(req, timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()

            self.assertTrue(payload["ok"])
            saved = config_path.read_text(encoding="utf-8")
            self.assertIn("- ws-a", saved)
            self.assertIn("- ws-b", saved)
            self.assertIn("format: 9router", saved)
            self.assertIn("alias_enabled: true", saved)


if __name__ == "__main__":
    unittest.main()
