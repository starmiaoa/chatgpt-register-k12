from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chatgpt_register_k12.cli import _prepare_run_archive
from chatgpt_register_k12.login.login_flow import (
    _extract_code_from_response,
    _page_type_from_response_url,
)
from chatgpt_register_k12.pipeline import (
    _apply_workspace_export_context,
    _fetch_account_context,
    run_export,
    run_full_pipeline,
    run_register,
    run_refresh_tokens,
)
from chatgpt_register_k12.register.registrar import (
    PROJECT_REGISTRATION_PASSWORD,
    PlatformRegistrar,
    normalize_registration_password,
)
from chatgpt_register_k12.register.mail_provider import (
    GmailPasswordProvider,
    OutlookTokenProvider,
    _entry_has_address,
    _fill_mailbox_credentials,
    _make_config,
    _message_matches_mailbox,
    _plus_alias_address,
    configured_mailboxes,
    mark_mailbox_result,
    parse_gmail_password_credentials,
)
from chatgpt_register_k12.workspace.joiner import join_workspace


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        text: str = "",
        *,
        url: str = "",
        headers: dict[str, str] | None = None,
        json_data=None,
        ok: bool | None = None,
        history: list["FakeResponse"] | None = None,
    ):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = headers or {}
        self._json_data = json_data
        self.ok = (200 <= status_code < 300) if ok is None else ok
        self.history = history or []

    def json(self):
        if self._json_data is None:
            raise ValueError("not json")
        return self._json_data


class WorkspaceCheckSession:
    def __init__(self, workspace_id: str = "workspace-id"):
        self.workspace_id = workspace_id

    def post(self, *args, **kwargs):
        return FakeResponse(
            200,
            "{}",
            json_data={
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
            },
        )

    def get(self, *args, **kwargs):
        return FakeResponse(
            200,
            "{}",
            json_data={
                "accounts": {
                    "default": {
                        "account": {
                            "id": self.workspace_id,
                            "plan_type": "k12",
                            "account_user_role": "standard-user",
                        }
                    }
                }
            },
        )

    def close(self):
        pass


class FakeCheckSession:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *args, **kwargs):
        return FakeResponse(200, text="{}", json_data=self.payload)


class FakeHealthSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.calls: list[dict] = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class FakeJoinSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class OfflineFlowTests(unittest.TestCase):
    def test_project_registration_password_is_fixed_and_valid(self):
        self.assertEqual(PROJECT_REGISTRATION_PASSWORD, "#A1234567890")
        self.assertEqual(
            normalize_registration_password(PROJECT_REGISTRATION_PASSWORD),
            PROJECT_REGISTRATION_PASSWORD,
        )
        registrar = PlatformRegistrar(mail_config={})
        try:
            self.assertEqual(registrar.account_password, PROJECT_REGISTRATION_PASSWORD)
        finally:
            registrar.close()

    def test_registration_password_requires_twelve_characters(self):
        self.assertEqual(
            normalize_registration_password("FixedPassword123!"),
            "FixedPassword123!",
        )
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            normalize_registration_password("")
        with self.assertRaisesRegex(ValueError, "at least 12"):
            normalize_registration_password("short")

    def test_run_register_does_not_accept_user_configured_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_file = Path(tmp) / "registered_accounts.json"
            config = {
                "_config_dir": tmp,
                "registration": {
                    "total": 1,
                    "threads": 1,
                    "password": "UserConfigured123!",
                },
                "proxy": {"url": ""},
                "mail": {},
            }
            seen_kwargs = []

            def fake_register_worker(**kwargs):
                seen_kwargs.append(kwargs)
                return {
                    "ok": True,
                    "index": kwargs["index"],
                    "cost_seconds": 0.1,
                    "result": {
                        "email": "user@example.com",
                        "password": PROJECT_REGISTRATION_PASSWORD,
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                        "id_token": "id-token",
                    },
                }

            with patch(
                "chatgpt_register_k12.pipeline.register_worker",
                side_effect=fake_register_worker,
            ):
                accounts = run_register(config, accounts_file)

            self.assertEqual(len(seen_kwargs), 1)
            self.assertNotIn("account_password", seen_kwargs[0])
            self.assertEqual(accounts[0]["password"], PROJECT_REGISTRATION_PASSWORD)

    def test_run_archive_paths_use_timestamped_count_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "_config_dir": tmp,
                "registration": {"total": 9},
                "sub2api": {"output_file": "sub2api_bundle.json"},
                "output": {"archive_runs": True, "runs_dir": "runs"},
                "logging": {"file": "test_run.log"},
            }
            args = SimpleNamespace(
                count=7,
                accounts=None,
                output="sub2api_bundle.json",
            )
            run_dir, accounts_file, output_file = _prepare_run_archive(config, args)

            self.assertIsNotNone(run_dir)
            assert run_dir is not None
            self.assertEqual(run_dir.parent, Path(tmp) / "runs")
            self.assertRegex(run_dir.name, r"^\d{8}-\d{6}_7_accounts$")
            self.assertTrue(run_dir.exists())
            self.assertEqual(accounts_file, run_dir / "registered_accounts.json")
            self.assertEqual(output_file, run_dir / "sub2api_bundle.json")
            self.assertEqual(config["logging"]["file"], str(run_dir / "test_run.log"))

    def test_run_archive_uses_selected_export_default_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "_config_dir": tmp,
                "registration": {"total": 1},
                "export": {"format": "cpa", "output_file": ""},
                "sub2api": {"output_file": "sub2api_bundle.json"},
                "output": {"archive_runs": True, "runs_dir": "runs"},
                "logging": {"file": ""},
            }
            args = SimpleNamespace(
                count=1,
                accounts=None,
                output=None,
            )
            run_dir, _, output_file = _prepare_run_archive(config, args)

            self.assertIsNotNone(run_dir)
            assert run_dir is not None
            self.assertEqual(output_file, run_dir / "cpa.json")

    def test_extract_code_from_redirect_history(self):
        redirect = FakeResponse(
            302,
            url="https://auth.openai.com/start",
            headers={
                "Location": "https://platform.openai.com/auth/callback?code=abc123&state=s"
            },
        )
        resp = FakeResponse(200, url="https://platform.openai.com/", history=[redirect])
        self.assertEqual(_extract_code_from_response(resp), "abc123")

    def test_extract_code_from_embedded_html(self):
        resp = FakeResponse(
            200,
            text='location.href="https%3A%2F%2Fplatform.openai.com%2Fauth%2Fcallback%3Fcode%3Dxyz789"',
        )
        self.assertEqual(_extract_code_from_response(resp), "xyz789")

    def test_infers_login_password_page(self):
        resp = FakeResponse(200, url="https://auth.openai.com/log-in/password")
        self.assertEqual(_page_type_from_response_url(resp), "login_password")

    def test_join_retry_keeps_original_request_body(self):
        session = FakeJoinSession(
            [
                FakeResponse(500, text="temporary", ok=False),
                FakeResponse(200, text="ok", ok=True),
            ]
        )
        result = join_workspace(
            access_token="token",
            workspace_id="workspace",
            route="k12_request",
            max_retries=2,
            retry_backoff_ms=0,
            session=session,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(all("data" not in call for call in session.calls))

    def test_join_retry_keeps_standard_empty_body(self):
        session = FakeJoinSession(
            [
                FakeResponse(500, text="temporary", ok=False),
                FakeResponse(200, text="ok", ok=True),
            ]
        )
        result = join_workspace(
            access_token="token",
            workspace_id="workspace",
            route="request",
            max_retries=2,
            retry_backoff_ms=0,
            session=session,
        )
        self.assertTrue(result["ok"])
        self.assertEqual([call.get("data") for call in session.calls], ["", ""])

    def test_fetch_account_context_prefers_target_workspace(self):
        payload = {
            "accounts": {
                "default": {
                    "account": {
                        "account_id": "personal",
                        "plan_type": "free",
                        "account_user_role": "account-owner",
                    }
                },
                "items": [
                    {
                        "account": {
                            "account_id": "workspace",
                            "plan_type": "k12",
                            "account_user_role": "member",
                        }
                    }
                ],
            }
        }
        context = _fetch_account_context(
            FakeCheckSession(payload),
            "token",
            workspace_id="workspace",
        )
        self.assertEqual(context["plan_type"], "k12")
        self.assertEqual(context["chatgpt_account_id"], "workspace")

    def test_fetch_account_context_reports_non_json_body(self):
        class Session:
            def get(self, *args, **kwargs):
                return FakeResponse(200, text="<html>blocked</html>", json_data=None)

        with self.assertRaisesRegex(RuntimeError, "returned non-JSON"):
            _fetch_account_context(Session(), "token")

    def test_team_export_filters_unverified_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "_config_dir": tmp,
                "workspace": {"re_login_enabled": True},
                "sub2api": {
                    "output_file": "bundle.json",
                    "require_team_tokens": "auto",
                    "health_check": False,
                },
            }
            accounts = [
                {
                    "email": "free@example.com",
                    "access_token": "free-at",
                    "refresh_token": "free-rt",
                    "plan_type": "free",
                    "team_login_status": "failed",
                },
                {
                    "email": "team@example.com",
                    "access_token": "old-at",
                    "refresh_token": "old-rt",
                    "plan_type": "k12",
                    "team_login_status": "ok",
                    "team_access_token": "team-at",
                    "team_refresh_token": "team-rt",
                    "team_id_token": "team-id",
                    "team_plan_type": "k12",
                    "team_chatgpt_account_id": "workspace",
                },
            ]
            json_str, actual_path = run_export(config, accounts)
            bundle = json.loads(json_str)
            self.assertEqual(Path(actual_path).name, "bundle.json")
            self.assertEqual(len(bundle["accounts"]), 1)
            creds = bundle["accounts"][0]["credentials"]
            self.assertEqual(creds["access_token"], "team-at")
            self.assertEqual(creds["plan_type"], "k12")

    def test_export_supports_cpa_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "_config_dir": tmp,
                "export": {"format": "cpa", "output_file": ""},
                "sub2api": {"health_check": False},
            }
            account = {
                "email": "cpa@example.com",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "plan_type": "k12",
                "chatgpt_account_id": "workspace-id",
                "chatgpt_user_id": "user-id",
            }

            json_str, actual_path = run_export(config, [account])

            self.assertEqual(Path(actual_path).name, "cpa.json")
            data = json.loads(json_str)
            self.assertIsInstance(data, list)
            self.assertEqual(data[0]["email"], "cpa@example.com")
            self.assertEqual(data[0]["chatgpt_account_id"], "workspace-id")
            self.assertEqual(data[0]["plan_type"], "k12")

    def test_export_auth_single_account_is_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "_config_dir": tmp,
                "export": {"format": "auth", "output_file": ""},
                "sub2api": {"health_check": False},
            }
            account = {
                "email": "auth@example.com",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "plan_type": "k12",
                "chatgpt_account_id": "workspace-id",
            }

            json_str, actual_path = run_export(config, [account])

            self.assertEqual(Path(actual_path).name, "auth.json")
            data = json.loads(json_str)
            self.assertIsInstance(data, dict)
            self.assertEqual(data["auth_mode"], "chatgpt")
            self.assertEqual(data["tokens"]["account_id"], "workspace-id")

    def test_team_export_fails_when_no_verified_team_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "_config_dir": tmp,
                "workspace": {"re_login_enabled": True},
                "sub2api": {
                    "output_file": "bundle.json",
                    "require_team_tokens": "auto",
                    "health_check": False,
                },
            }
            with self.assertRaises(RuntimeError):
                run_export(
                    config,
                    [
                        {
                            "email": "free@example.com",
                            "access_token": "free-at",
                            "plan_type": "free",
                            "team_login_status": "failed",
                        }
                    ],
                )

    def test_workspace_join_context_exports_as_k12(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "_config_dir": tmp,
                "workspace": {"re_login_enabled": True},
                "sub2api": {
                    "output_file": "bundle.json",
                    "require_team_tokens": "auto",
                    "health_check": False,
                },
            }
            account = {
                "email": "joined@example.com",
                "access_token": "joined-at",
                "refresh_token": "joined-rt",
                "id_token": "joined-id",
                "plan_type": "free",
                "chatgpt_account_id": "personal",
                "workspace_membership_active": True,
            }
            _apply_workspace_export_context(account, "workspace-id", "k12")
            json_str, _ = run_export(config, [account])
            bundle = json.loads(json_str)
            creds = bundle["accounts"][0]["credentials"]
            self.assertEqual(creds["access_token"], "joined-at")
            self.assertEqual(creds["chatgpt_account_id"], "workspace-id")
            self.assertEqual(creds["plan_type"], "k12")
            self.assertEqual(bundle["accounts"][0]["extra"]["source"], "workspace_join")

    def test_checked_k12_context_exports_without_workspace_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "_config_dir": tmp,
                "workspace": {"re_login_enabled": False},
                "sub2api": {
                    "output_file": "bundle.json",
                    "require_team_tokens": True,
                    "health_check": False,
                },
            }
            account = {
                "email": "checked@example.com",
                "access_token": "checked-at",
                "refresh_token": "checked-rt",
                "id_token": "checked-id",
                "plan_type": "k12",
                "chatgpt_account_id": "workspace-id",
                "account_user_role": "standard-user",
                "workspace_membership_active": True,
            }
            json_str, _ = run_export(config, [account])
            bundle = json.loads(json_str)
            creds = bundle["accounts"][0]["credentials"]
            self.assertEqual(creds["access_token"], "checked-at")
            self.assertEqual(creds["chatgpt_account_id"], "workspace-id")
            self.assertEqual(creds["plan_type"], "k12")
            self.assertEqual(bundle["accounts"][0]["extra"]["source"], "workspace_check")

    def test_refresh_check_repairs_join_false_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            account = {
                "email": "checked@example.com",
                "access_token": "old-access-token",
                "refresh_token": "old-refresh-token",
                "join_status": "failed",
                "join_results": [
                    {
                        "ok": False,
                        "workspace_id": "workspace-id",
                        "error": "Must be part of this workspace to perform this action",
                    }
                ],
                "workspace_membership_active": False,
            }
            config = {
                "_config_dir": tmp,
                "proxy": {"url": ""},
                "workspace": {"ids": ["workspace-id"]},
                "workspace_state": {
                    "file": str(Path(tmp) / "workspace_account_state.json")
                },
                "parallel": {"refresh_threads": 1},
            }

            with patch(
                "chatgpt_register_k12.pipeline._create_http_session",
                return_value=WorkspaceCheckSession("workspace-id"),
            ):
                refreshed = run_refresh_tokens(config, [account])

            fixed = refreshed[0]
            self.assertEqual(fixed["join_status"], "ok")
            self.assertTrue(fixed["workspace_membership_active"])
            self.assertEqual(fixed["workspace_export_status"], "ok")
            self.assertEqual(fixed["plan_type"], "k12")
            self.assertEqual(fixed["chatgpt_account_id"], "workspace-id")
            self.assertTrue(fixed["join_results"][0]["membership_active"])
            self.assertEqual(
                fixed["join_results"][0]["membership_detail"],
                "verified by accounts/check",
            )

    def test_export_skips_join_token_invalidated_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "_config_dir": tmp,
                "workspace": {"re_login_enabled": False},
                "sub2api": {
                    "output_file": "bundle.json",
                    "require_team_tokens": True,
                    "health_check": False,
                },
            }
            good = {
                "email": "good@example.com",
                "access_token": "good-at",
                "refresh_token": "good-rt",
                "plan_type": "k12",
                "chatgpt_account_id": "workspace-id",
                "refresh_status": "ok",
            }
            bad = {
                "email": "bad@example.com",
                "access_token": "bad-at",
                "refresh_token": "bad-rt",
                "plan_type": "k12",
                "chatgpt_account_id": "workspace-id",
                "refresh_status": "ok",
                "join_results": [
                    {
                        "ok": False,
                        "body": '{"error":{"code":"token_invalidated"}}',
                    }
                ],
            }

            json_str, _ = run_export(config, [good, bad])
            bundle = json.loads(json_str)
            self.assertEqual(len(bundle["accounts"]), 1)
            self.assertEqual(bundle["accounts"][0]["name"], "good@example.com")

    def test_export_health_check_filters_401_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "_config_dir": tmp,
                "workspace": {"re_login_enabled": False},
                "sub2api": {
                    "output_file": "bundle.json",
                    "require_team_tokens": True,
                    "health_check": True,
                    "health_check_endpoint": "models",
                    "health_check_retries": 1,
                    "health_check_delay_seconds": 0,
                },
            }
            good = {
                "email": "good@example.com",
                "access_token": "good-at",
                "refresh_token": "good-rt",
                "plan_type": "k12",
                "chatgpt_account_id": "workspace-id",
                "refresh_status": "ok",
            }
            bad = {
                "email": "bad@example.com",
                "access_token": "bad-at",
                "refresh_token": "bad-rt",
                "plan_type": "k12",
                "chatgpt_account_id": "workspace-id",
                "refresh_status": "ok",
            }
            session = FakeHealthSession(
                [
                    FakeResponse(200, text="{}", json_data={}),
                    FakeResponse(
                        401,
                        text='{"error":{"code":"token_invalidated"}}',
                        json_data={"error": {"code": "token_invalidated"}},
                        ok=False,
                    ),
                ]
            )

            with patch(
                "chatgpt_register_k12.pipeline._create_http_session",
                return_value=session,
            ):
                json_str, _ = run_export(config, [good, bad])

            bundle = json.loads(json_str)
            self.assertEqual(len(bundle["accounts"]), 1)
            self.assertEqual(bundle["accounts"][0]["name"], "good@example.com")
            self.assertEqual(good["export_health_status"], "ok")
            self.assertEqual(bad["export_health_status"], "failed")
            self.assertTrue(session.closed)

    def test_full_pipeline_summary_uses_actual_export_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = [
                {
                    "email": "good@example.com",
                    "access_token": "good-at",
                    "refresh_token": "good-rt",
                    "plan_type": "k12",
                    "chatgpt_account_id": "workspace-id",
                    "refresh_status": "ok",
                },
                {
                    "email": "bad@example.com",
                    "access_token": "bad-at",
                    "refresh_token": "bad-rt",
                    "plan_type": "k12",
                    "chatgpt_account_id": "workspace-id",
                    "refresh_status": "failed",
                },
            ]
            config = {
                "_config_dir": tmp,
                "workspace": {"re_login_enabled": False},
                "sub2api": {
                    "output_file": "bundle.json",
                    "require_team_tokens": True,
                    "health_check": False,
                },
            }
            accounts_file = str(Path(tmp) / "registered_accounts.json")
            output_file = str(Path(tmp) / "bundle.json")

            with patch(
                "chatgpt_register_k12.pipeline.run_register",
                return_value=accounts,
            ), patch(
                "chatgpt_register_k12.pipeline.run_join_workspace",
                return_value=accounts,
            ), patch(
                "chatgpt_register_k12.pipeline.run_refresh_tokens",
                return_value=accounts,
            ):
                summary = run_full_pipeline(
                    config,
                    count=2,
                    accounts_file=accounts_file,
                    output_file=output_file,
                )

            self.assertEqual(summary["registered"], 2)
            self.assertEqual(summary["refreshed"], 1)
            self.assertEqual(summary["exported"], 1)

    def test_outlook_plus_alias_addresses(self):
        self.assertEqual(
            _plus_alias_address("aliasuser@example.com", 0),
            "aliasuser@example.com",
        )
        self.assertEqual(
            _plus_alias_address("aliasuser@example.com", 1),
            "aliasuser+1@example.com",
        )
        self.assertEqual(
            _plus_alias_address("aliasuser@example.com", 5),
            "aliasuser+5@example.com",
        )

    def test_outlook_alias_pool_uses_main_plus_five_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = {
                "type": "outlook_token",
                "enable": True,
                "alias_enabled": True,
                "alias_limit_per_mailbox": 6,
                "mailboxes": (
                    "aliasuser@example.com----pw----client-id----refresh-token"
                ),
            }
            provider = OutlookTokenProvider(
                entry,
                _make_config(
                    {
                        "_config_dir": tmp,
                        "request_timeout": 1,
                        "wait_timeout": 1,
                        "wait_interval": 1,
                    }
                ),
            )
            try:
                addresses = []
                for _ in range(6):
                    mailbox = provider.create_mailbox()
                    addresses.append(mailbox["address"])
                    mark_mailbox_result(mailbox, success=True)

                self.assertEqual(
                    addresses,
                    [
                        "aliasuser@example.com",
                        "aliasuser+1@example.com",
                        "aliasuser+2@example.com",
                        "aliasuser+3@example.com",
                        "aliasuser+4@example.com",
                        "aliasuser+5@example.com",
                    ],
                )
                with self.assertRaises(RuntimeError):
                    provider.create_mailbox()
            finally:
                provider.close()

    def test_outlook_failed_alias_can_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = {
                "type": "outlook_token",
                "enable": True,
                "alias_enabled": True,
                "alias_limit_per_mailbox": 2,
                "mailboxes": (
                    "retryuser@example.com----pw----client-id----refresh-token"
                ),
            }
            provider = OutlookTokenProvider(
                entry,
                _make_config(
                    {
                        "_config_dir": tmp,
                        "request_timeout": 1,
                        "wait_timeout": 1,
                        "wait_interval": 1,
                    }
                ),
            )
            try:
                first = provider.create_mailbox()
                mark_mailbox_result(first, success=False, error="temporary failure")
                second = provider.create_mailbox()

                self.assertEqual(second["address"], first["address"])
            finally:
                provider.close()

    def test_gmail_app_password_parser_removes_spaces(self):
        credentials = parse_gmail_password_credentials(
            "user@gmail.com----abcd efgh ijkl mnop\n"
        )

        self.assertEqual(credentials[0]["email"], "user@gmail.com")
        self.assertEqual(credentials[0]["app_password"], "abcdefghijklmnop")

    def test_gmail_password_candidates_use_aliases(self):
        candidates = configured_mailboxes(
            {
                "providers": [
                    {
                        "type": "gmail_password",
                        "enable": True,
                        "alias_enabled": True,
                        "alias_limit_per_mailbox": 3,
                        "mailboxes": "user@gmail.com----abcd efgh ijkl mnop",
                    }
                ],
                "alias_enabled": True,
                "alias_limit_per_mailbox": 3,
            }
        )

        self.assertEqual(
            [item["address"] for item in candidates],
            [
                "user@gmail.com",
                "user+1@gmail.com",
                "user+2@gmail.com",
            ],
        )
        self.assertEqual(candidates[0]["provider"], "gmail_password")
        self.assertEqual(candidates[0]["login_address"], "user@gmail.com")

    def test_gmail_password_pool_uses_main_plus_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = {
                "type": "gmail_password",
                "enable": True,
                "alias_enabled": True,
                "alias_limit_per_mailbox": 2,
                "mailboxes": "user@gmail.com----abcd efgh ijkl mnop",
            }
            provider = GmailPasswordProvider(
                entry,
                _make_config(
                    {
                        "_config_dir": tmp,
                        "request_timeout": 1,
                        "wait_timeout": 1,
                        "wait_interval": 1,
                    }
                ),
            )
            try:
                first = provider.create_mailbox()
                mark_mailbox_result(first, success=True)
                second = provider.create_mailbox()

                self.assertEqual(first["address"], "user@gmail.com")
                self.assertEqual(second["address"], "user+1@gmail.com")
                self.assertEqual(second["login_address"], "user@gmail.com")
                self.assertEqual(second["app_password"], "abcdefghijklmnop")
            finally:
                provider.close()

    def test_outlook_alias_matches_base_credential(self):
        entry = {
            "type": "outlook_token",
            "mailboxes": "aliasuser@example.com----pw----client-id----refresh-token",
        }
        mailbox = {
            "provider": "outlook_token",
            "address": "aliasuser+2@example.com",
        }
        self.assertTrue(_entry_has_address(entry, mailbox["address"]))

        _fill_mailbox_credentials(mailbox, entry)
        self.assertEqual(mailbox["client_id"], "client-id")
        self.assertEqual(mailbox["refresh_token"], "refresh-token")
        self.assertEqual(mailbox["base_address"], "aliasuser@example.com")
        self.assertEqual(mailbox["login_address"], "aliasuser@example.com")

    def test_gmail_password_alias_fills_app_password_only(self):
        entry = {
            "type": "gmail_password",
            "mailboxes": "aliasuser@gmail.com----abcd efgh ijkl mnop",
        }
        mailbox = {
            "provider": "gmail_password",
            "address": "aliasuser+2@gmail.com",
        }

        self.assertTrue(_entry_has_address(entry, mailbox["address"]))
        _fill_mailbox_credentials(mailbox, entry)

        self.assertEqual(mailbox["app_password"], "abcdefghijklmnop")
        self.assertEqual(mailbox["base_address"], "aliasuser@gmail.com")
        self.assertEqual(mailbox["login_address"], "aliasuser@gmail.com")
        self.assertNotIn("client_id", mailbox)
        self.assertNotIn("refresh_token", mailbox)

    def test_alias_message_matching_uses_recipient(self):
        mailbox = {"address": "aliasuser+2@example.com"}
        self.assertTrue(
            _message_matches_mailbox(
                mailbox,
                {"recipients": ["aliasuser+2@example.com"]},
            )
        )
        self.assertFalse(
            _message_matches_mailbox(
                mailbox,
                {"recipients": ["aliasuser+3@example.com"]},
            )
        )
        self.assertTrue(_message_matches_mailbox(mailbox, {"recipients": []}))


if __name__ == "__main__":
    unittest.main()
