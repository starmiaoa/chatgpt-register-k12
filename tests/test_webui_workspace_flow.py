from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.request import Request, urlopen

import yaml

from chatgpt_register_k12.login.login_flow import (
    LoginError,
    PasswordRequiredError,
    _complete_login_flow,
    _send_login_otp,
)
from chatgpt_register_k12.pipeline import run_export, run_login_existing
from chatgpt_register_k12.register.registrar import PROJECT_REGISTRATION_PASSWORD
from chatgpt_register_k12.webui.terminal import run_terminal_command
from chatgpt_register_k12.webui.redact import redact_object, redact_text
from chatgpt_register_k12.webui.jobs import JobManager
from chatgpt_register_k12.webui.server import WebUIHandler
from chatgpt_register_k12.workspace_state import (
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


class HtmlRefreshThenHealthySession:
    def __init__(self):
        self.get_calls = 0
        self.post_calls = 0
        self.post_urls: list[str] = []

    def get(self, *args, **kwargs):
        self.get_calls += 1
        if self.get_calls == 1:
            return FakeResponse(401, "plain unauthorized body")
        return FakeResponse(200, "{}", {})

    def post(self, url, **kwargs):
        self.post_calls += 1
        self.post_urls.append(url)
        if self.post_calls == 1:
            return FakeResponse(200, "<!DOCTYPE html><html>auth page</html>")
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

    def test_stale_in_use_workspace_email_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace_account_state.json"
            set_workspace_email_state(path, "ws-a", "user@outlook.com", "in_use")
            data = json.loads(path.read_text(encoding="utf-8"))
            data["workspaces"]["ws-a"]["user@outlook.com"]["updated_at"] = (
                datetime.now(timezone.utc) - timedelta(minutes=6)
            ).isoformat()
            path.write_text(json.dumps(data), encoding="utf-8")

            self.assertTrue(workspace_email_available(path, "ws-a", "user@outlook.com"))


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

    def test_existing_sent_otp_is_not_sent_twice(self):
        validate_response = {
            "continue_url": "https://platform.openai.com/auth/callback?code=ok-code"
        }
        with patch("chatgpt_register_k12.login.login_flow._send_login_otp") as send_otp:
            with patch(
                "chatgpt_register_k12.login.login_flow._validate_login_otp",
                return_value=validate_response,
            ) as validate_otp:
                code, verifier = _complete_login_flow(
                    session=object(),
                    device_id="device",
                    email="user+1@example.com",
                    mail_config={},
                    workspace_id="",
                    response_data={
                        "page": {"type": "email_otp_verification"},
                        "_otp_already_sent": True,
                        "_otp_not_before": datetime.now(timezone.utc),
                    },
                    code_verifier="verifier",
                    password=None,
                )

        self.assertEqual(code, "ok-code")
        self.assertEqual(verifier, "verifier")
        send_otp.assert_not_called()
        validate_otp.assert_called_once()

    def test_otp_send_password_page_requires_password(self):
        response = FakeResponse(
            200,
            "<html>Password</html>",
            json_data=None,
        )
        response.url = "https://auth.openai.com/log-in/password"
        with patch(
            "chatgpt_register_k12.login.login_flow.request_with_retry",
            return_value=(response, None),
        ):
            with self.assertRaises(PasswordRequiredError):
                _send_login_otp(object())

    def test_otp_send_error_page_fails_without_waiting(self):
        response = FakeResponse(
            200,
            "<html>Auth error</html>",
            json_data=None,
        )
        response.url = "https://auth.openai.com/error?payload=x&session_id=y"
        with patch(
            "chatgpt_register_k12.login.login_flow.request_with_retry",
            return_value=(response, None),
        ):
            with self.assertRaises(LoginError):
                _send_login_otp(object())

    def test_otp_send_error_page_retries_after_email_verification_navigation(self):
        error_response = FakeResponse(200, "<html>Auth error</html>", json_data=None)
        error_response.url = "https://auth.openai.com/error?payload=x&session_id=y"
        verification_response = FakeResponse(
            200,
            "<html>Email verification</html>",
            json_data=None,
        )
        verification_response.url = "https://auth.openai.com/email-verification"
        calls = []

        def fake_request(_session, _method, url, **_kwargs):
            calls.append(url)
            if len(calls) == 1:
                return error_response, None
            return verification_response, None

        with patch(
            "chatgpt_register_k12.login.login_flow.request_with_retry",
            side_effect=fake_request,
        ):
            _send_login_otp(object())

        self.assertEqual(
            calls,
            [
                "https://auth.openai.com/api/accounts/email-otp/send",
                "https://auth.openai.com/email-verification",
                "https://auth.openai.com/api/accounts/email-otp/send",
            ],
        )


class ExistingLoginFallbackTests(unittest.TestCase):
    def test_existing_login_falls_back_to_project_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_file = Path(tmp) / "registered_accounts.json"
            seen = {}

            def fake_password_login(**kwargs):
                seen.update(kwargs)
                return {
                    "access_token": "password-access-token",
                    "refresh_token": "password-refresh-token",
                    "id_token": "password-id-token",
                    "password": kwargs["password"],
                    "created_at": "now",
                }

            with patch(
                "chatgpt_register_k12.pipeline.configured_mailboxes",
                return_value=[{"address": "user@example.com", "provider": "gmail_password"}],
            ), patch(
                "chatgpt_register_k12.pipeline.re_login_email_otp_for_team_token",
                side_effect=PasswordRequiredError("password is required"),
            ), patch(
                "chatgpt_register_k12.pipeline.re_login_for_team_token",
                side_effect=fake_password_login,
            ):
                accounts = run_login_existing(
                    {
                        "_config_dir": tmp,
                        "mail": {},
                        "proxy": {"url": ""},
                        "parallel": {"login_threads": 1},
                        "workspace": {"ids": []},
                    },
                    accounts_file,
                    count=1,
                )

            self.assertEqual(len(accounts), 1)
            self.assertEqual(seen["password"], PROJECT_REGISTRATION_PASSWORD)
            self.assertEqual(accounts[0]["password"], PROJECT_REGISTRATION_PASSWORD)
            self.assertEqual(accounts[0]["access_token"], "password-access-token")

    def test_existing_login_error_page_falls_back_to_project_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_file = Path(tmp) / "registered_accounts.json"

            with patch(
                "chatgpt_register_k12.pipeline.configured_mailboxes",
                return_value=[{"address": "user@example.com", "provider": "gmail_password"}],
            ), patch(
                "chatgpt_register_k12.pipeline.re_login_email_otp_for_team_token",
                side_effect=LoginError("Unsupported login continuation page: error"),
            ), patch(
                "chatgpt_register_k12.pipeline.re_login_for_team_token",
                return_value={
                    "access_token": "password-access-token",
                    "refresh_token": "password-refresh-token",
                    "id_token": "password-id-token",
                    "password": PROJECT_REGISTRATION_PASSWORD,
                    "created_at": "now",
                },
            ) as password_login:
                accounts = run_login_existing(
                    {
                        "_config_dir": tmp,
                        "mail": {},
                        "proxy": {"url": ""},
                        "parallel": {"login_threads": 1},
                        "workspace": {"ids": []},
                    },
                    accounts_file,
                    count=1,
                )

            self.assertEqual(len(accounts), 1)
            password_login.assert_called_once()


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
                "chatgpt_register_k12.pipeline._create_http_session",
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

    def test_export_health_refresh_fallback_uses_browser_endpoint_after_html(self):
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
            session = HtmlRefreshThenHealthySession()
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
                "chatgpt_register_k12.pipeline._create_http_session",
                return_value=session,
            ):
                json_str, _ = run_export(config, [account], out)

            data = json.loads(json_str)
            self.assertEqual(len(data["accounts"]), 1)
            self.assertEqual(account["access_token"], "new-access-token")
            self.assertEqual(account["refresh_token"], "new-refresh-token")
            self.assertEqual(session.post_calls, 2)
            self.assertTrue(session.post_urls[0].endswith("/oauth/token"))
            self.assertTrue(session.post_urls[1].endswith("/api/accounts/oauth/token"))


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

    def test_config_save_can_switch_to_gmail_app_password(self):
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
                        "mail_provider": "gmail",
                        "gmail_mailboxes": "user@gmail.com----abcd efgh ijkl mnop",
                        "outlook_mailboxes": "user@outlook.com----pw----cid----rt",
                        "alias_enabled": True,
                        "alias_limit_per_mailbox": 6,
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
            self.assertNotIn("abcdefghijklmnop", json.dumps(payload))
            saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            providers = saved["mail"]["providers"]
            outlook = next(item for item in providers if item["type"] == "outlook_token")
            gmail = next(item for item in providers if item["type"] == "gmail_password")
            self.assertFalse(outlook["enable"])
            self.assertTrue(gmail["enable"])
            self.assertTrue(gmail["alias_enabled"])
            self.assertEqual(gmail["alias_limit_per_mailbox"], 6)
            self.assertEqual(
                gmail["mailboxes"],
                "user@gmail.com----abcd efgh ijkl mnop",
            )

    def test_config_save_ignores_registration_password_from_request(self):
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
                        "registration_password": "UserConfigured123!",
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
            self.assertNotIn("UserConfigured123!", json.dumps(payload))
            saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertNotIn("password", saved.get("registration", {}))


if __name__ == "__main__":
    unittest.main()
