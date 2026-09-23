"""Integration checks for local features on the upstream storage and auth flows."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from core import cloakbrowser_registration as cloak
from core import codex_oauth as oauth
from core import db, sms_provider


class OAuthMergeTests(unittest.TestCase):
    def run_flow(self, initial, otp_result=None, **overrides):
        callback = "http://localhost:1455/auth/callback?code=abc&state=state-test"
        mocks = {
            "BrowserSession": Mock(return_value=Mock()),
            "_request_cpa_authorize_url": Mock(return_value={
                "state": "state-test", "auth_url": "https://auth.openai.com/oauth/authorize",
            }),
            "_codex_auth_preflight": Mock(),
            "human_delay": Mock(),
            "_bootstrap_authorize": Mock(),
            "_submit_email": Mock(return_value=initial),
            "_submit_email_otp": Mock(return_value=otp_result or {}),
            "_account_registration_password": Mock(return_value=""),
            "_known_codex_phone_verified": Mock(return_value=True),
            "_do_phone_verification": Mock(),
            "_select_workspace_and_get_callback": Mock(return_value=callback),
            "_submit_cpa_callback": Mock(return_value={"message": "uploaded"}),
            "_save_cpa_local_record": Mock(return_value=None),
            "_mark_codex_phone_verified": Mock(),
        }
        mocks.update(overrides)
        otp = Mock(return_value="123456")
        with ExitStack() as stack:
            stack.enter_context(patch.object(oauth._cfg, "CODEX_OAUTH_DRIVER", "protocol"))
            stack.enter_context(patch.object(oauth._cfg, "CODEX_AUTH_URL_SOURCE", "cpa"))
            for name, mock in mocks.items():
                stack.enter_context(patch.object(oauth, name, mock))
            result = oauth.run_codex_oauth("merge@example.test", otp_provider=otp, force=True)
        self.assertTrue(result["ok"], result)
        mocks["_submit_cpa_callback"].assert_called_once_with(callback)
        mocks["_mark_codex_phone_verified"].assert_called_once_with("merge@example.test", "oauth_callback")
        return mocks, otp

    def test_otp_submitted_once_then_mfa_and_explicit_phone_override_local_record(self):
        mfa = Mock(return_value={"continue_url": "https://auth.openai.com/add-phone"})
        mocks, otp = self.run_flow(
            {"page": {"type": "email_verification"}},
            {"page": {"type": "mfa_challenge"}, "factor_id": "factor-test"},
            _complete_mfa_if_required=mfa,
            _follow_login_continue=Mock(return_value=None),
        )
        otp.assert_called_once()
        mocks["_submit_email_otp"].assert_called_once()
        mfa.assert_called_once()
        mocks["_do_phone_verification"].assert_called_once()
        self.assertEqual(mocks["_do_phone_verification"].call_args.kwargs["email"], "merge@example.test")

    def test_password_mfa_direct_callback_does_not_request_email_or_sms(self):
        callback = "http://localhost:1455/auth/callback?code=abc&state=state-test"
        mocks, otp = self.run_flow(
            {"page": {"type": "login_password"}},
            _account_registration_password=Mock(return_value="test-password"),
            _password_verify=Mock(return_value={"page": {"type": "mfa_challenge"}}),
            _complete_mfa_if_required=Mock(return_value={"continue_url": callback}),
        )
        otp.assert_not_called()
        mocks["_submit_email_otp"].assert_not_called()
        mocks["_complete_mfa_if_required"].assert_called_once()
        mocks["_do_phone_verification"].assert_not_called()
        mocks["_select_workspace_and_get_callback"].assert_not_called()

    def test_consent_falls_back_to_sms_only_when_server_requires_phone(self):
        callback = "http://localhost:1455/auth/callback?code=abc&state=state-test"
        select = Mock(side_effect=[RuntimeError("phone_verification required"), callback])
        mocks, otp = self.run_flow(
            {"page": {"type": "consent"}}, _select_workspace_and_get_callback=select,
        )
        otp.assert_not_called()
        self.assertEqual(select.call_count, 2)
        mocks["_do_phone_verification"].assert_called_once()

    def test_nested_callback_after_otp_skips_workspace_and_sms(self):
        mocks, otp = self.run_flow(
            {"continue_url": "https://auth.openai.com/email-verification"},
            {"page": {"payload": {"url": "http://localhost:1455/auth/callback?code=abc&state=state-test"}}},
        )
        otp.assert_called_once()
        mocks["_submit_email_otp"].assert_called_once()
        mocks["_select_workspace_and_get_callback"].assert_not_called()
        mocks["_do_phone_verification"].assert_not_called()


class StorageMergeTests(unittest.TestCase):
    def test_json_migration_and_sqlite_updates_preserve_local_and_upstream_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = {name: root / name for name in (
                "_ACCOUNTS_JSON", "_OUTLOOK_JSON", "_GENERIC_API_EMAIL_JSON", "_DOMAIN_EMAIL_JSON",
                "_JOBS_JSON", "_LEGACY_ACCOUNTS_JSON", "_LEGACY_OUTLOOK_JSON", "_LEGACY_JOBS_JSON",
                "_LEGACY_SQLITE", "_CODEX_DIR", "_CODEX_AGENT_DIR", "_LEGACY_CODEX_EXPORT_STATE", "_LOG_DIR",
            )}
            legacy = {"id": 1, "email": "merge@example.test", "codex_phone_verified": True,
                      "gcash_eligibility_status": "success", "gcash_eligibility_ok": True,
                      "extra_json": json.dumps({"registration_password": "test-password"})}
            paths["_ACCOUNTS_JSON"].write_text(json.dumps([legacy]))
            with patch.multiple(db, **paths, _SQLITE_READY=False, _SQLITE_READY_PATH=None):
                self.assertTrue(db.get_account(1)["gcash_eligibility_ok"])
                self.assertTrue(db.is_account_codex_phone_verified(legacy["email"]))
                self.assertTrue(db.update_account_codex_phone_verified(legacy["email"], source="hero"))
                db.update_account_note(1, "preserved note")
                job = db.create_job("imap", proxy_used="socks5://example.test:1080", sms_snapshot={
                    "provider": "hero", "api_key": "secret-key", "api_key_configured": True,
                })
                db.update_job(job["id"], network_traffic={"total_bytes": 123})
                # A restart must read SQLite, without overwriting newer data from the old JSON.
                db._SQLITE_READY = False
                db._SQLITE_READY_PATH = None
                account = db.get_account(1)
                self.assertEqual(account["note"], "preserved note")
                self.assertEqual(account["codex_phone_verified_source"], "hero")
                self.assertTrue(account["gcash_eligibility_ok"])
                self.assertEqual(json.loads(account["extra_json"])["registration_password"], "test-password")
                saved_job = db.get_job(job["id"])
                self.assertEqual(saved_job["network_traffic"], {"total_bytes": 123})
                self.assertEqual(saved_job["proxy_used"], "socks5://example.test:1080")
                self.assertEqual(saved_job["sms_snapshot"], {"provider": "hero", "api_key_configured": True})
                self.assertEqual(json.loads(paths["_ACCOUNTS_JSON"].read_text()), [legacy])


class CloakMergeTests(unittest.TestCase):
    def test_email_callback_and_sms_context_survive_isolated_thread(self):
        settings = sms_provider.SmsTaskSettings.from_runtime()
        callback = Mock()

        def impl(**kwargs):
            self.assertIs(sms_provider.current_settings(), settings)
            self.assertEqual(kwargs["proxy"], "socks5://example.test:1080")
            kwargs["on_email_acquired"]("allocated@example.test")
            return {"success": True, "network_traffic": {"total_bytes": 123}}

        with sms_provider.bind_settings(settings), \
             patch.object(cloak, "_has_running_asyncio_loop", return_value=True), \
             patch.object(cloak, "_run_cloak_registration_impl", side_effect=impl):
            result = cloak.run_cloak_registration(
                None, "Test", "1990-01-01", proxy="socks5://example.test:1080", on_email_acquired=callback,
            )
        callback.assert_called_once_with("allocated@example.test")
        self.assertEqual(result["network_traffic"], {"total_bytes": 123})


if __name__ == "__main__":
    unittest.main()
