# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import Mock, patch

from core import codex_oauth, db


class _Response:
    def __init__(self, payload=None, *, url="https://auth.openai.com/email-verification", headers=None):
        self.payload = payload or {}
        self.status_code = 200
        self.url = url
        self.headers = headers or {}
        self.text = json.dumps(self.payload)

    def json(self):
        return self.payload


class CodexPhoneStateTests(unittest.TestCase):
    def test_email_otp_response_detects_phone_and_consent_states(self):
        phone = _Response({"continue_url": "https://auth.openai.com/add-phone"})
        consent = _Response({
            "page": {"type": "consent"},
            "continue_url": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        })
        unknown = _Response({"ok": True})

        self.assertIs(codex_oauth._email_otp_phone_requirement(phone), True)
        self.assertIs(codex_oauth._email_otp_phone_requirement(consent), False)
        self.assertIsNone(codex_oauth._email_otp_phone_requirement(unknown))

    def test_direct_callback_is_extracted_from_nested_response(self):
        callback = "http://localhost:1455/auth/callback?code=abc&state=state-1"
        response = _Response({"page": {"payload": {"url": callback}}})
        self.assertEqual(
            codex_oauth._extract_direct_callback_from_auth_response(response),
            callback,
        )

    def test_protocol_existing_phone_skips_sms_and_submits_cpa(self):
        state = "state-1"
        callback = f"http://localhost:1455/auth/callback?code=abc&state={state}"
        consent_response = _Response({
            "page": {"type": "consent"},
            "continue_url": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        })
        phone = Mock()
        submit_cpa = Mock(return_value={"message": "uploaded"})

        with patch.object(codex_oauth._cfg, "CODEX_OAUTH_DRIVER", "protocol"), \
             patch.object(codex_oauth._cfg, "CODEX_AUTH_URL_SOURCE", "cpa"), \
             patch.object(codex_oauth, "BrowserSession", return_value=object()), \
             patch.object(codex_oauth, "_request_cpa_authorize_url", return_value={
                 "state": state,
                 "auth_url": f"https://auth.openai.com/oauth/authorize?state={state}",
             }), \
             patch.object(codex_oauth, "network_preflight"), \
             patch.object(codex_oauth, "human_delay"), \
             patch.object(codex_oauth, "_bootstrap_authorize"), \
             patch.object(codex_oauth, "_submit_email"), \
             patch.object(codex_oauth, "_submit_email_otp", return_value=consent_response), \
             patch.object(codex_oauth, "_known_codex_phone_verified", return_value=False), \
             patch.object(codex_oauth, "_select_workspace_and_get_callback", return_value=callback), \
             patch.object(codex_oauth, "_do_phone_verification", phone), \
             patch.object(codex_oauth, "_submit_cpa_callback", submit_cpa), \
             patch.object(codex_oauth, "_save_cpa_local_record", return_value=None), \
             patch.object(codex_oauth, "_mark_codex_phone_verified"):
            result = codex_oauth.run_codex_oauth(
                "verified@example.com",
                otp_provider=lambda *_args, **_kwargs: "123456",
                force=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "success")
        phone.assert_not_called()
        submit_cpa.assert_called_once_with(callback)

    def test_protocol_explicit_phone_step_uses_sms_before_callback(self):
        state = "state-2"
        callback = f"http://localhost:1455/auth/callback?code=abc&state={state}"
        phone_response = _Response({"continue_url": "https://auth.openai.com/add-phone"})
        order = []

        def verify_phone(*_args, **_kwargs):
            order.append("phone")

        def select_callback(*_args, **_kwargs):
            order.append("callback")
            return callback

        with patch.object(codex_oauth._cfg, "CODEX_OAUTH_DRIVER", "protocol"), \
             patch.object(codex_oauth._cfg, "CODEX_AUTH_URL_SOURCE", "cpa"), \
             patch.object(codex_oauth, "BrowserSession", return_value=object()), \
             patch.object(codex_oauth, "_request_cpa_authorize_url", return_value={
                 "state": state,
                 "auth_url": f"https://auth.openai.com/oauth/authorize?state={state}",
             }), \
             patch.object(codex_oauth, "network_preflight"), \
             patch.object(codex_oauth, "human_delay"), \
             patch.object(codex_oauth, "_bootstrap_authorize"), \
             patch.object(codex_oauth, "_submit_email"), \
             patch.object(codex_oauth, "_submit_email_otp", return_value=phone_response), \
             patch.object(codex_oauth, "_known_codex_phone_verified", return_value=False), \
             patch.object(codex_oauth, "_do_phone_verification", side_effect=verify_phone), \
             patch.object(codex_oauth, "_select_workspace_and_get_callback", side_effect=select_callback), \
             patch.object(codex_oauth, "_submit_cpa_callback", return_value={"message": "uploaded"}), \
             patch.object(codex_oauth, "_save_cpa_local_record", return_value=None), \
             patch.object(codex_oauth, "_mark_codex_phone_verified"):
            result = codex_oauth.run_codex_oauth(
                "unverified@example.com",
                otp_provider=lambda *_args, **_kwargs: "123456",
                force=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(order, ["phone", "callback"])

    def test_db_phone_state_does_not_store_number_or_code(self):
        rows = [{"id": 1, "email": "verified@example.com"}]
        saved = []
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts", side_effect=lambda value: saved.append(value)), \
             patch.object(db, "_now", return_value="2026-08-18T19:00:00"):
            self.assertTrue(db.update_account_codex_phone_verified(
                "verified@example.com", True, source="hero"
            ))

        row = saved[-1][0]
        self.assertTrue(row["codex_phone_verified"])
        self.assertEqual(row["codex_phone_verified_source"], "hero")
        self.assertNotIn("phone", {key for key in row if key == "phone"})
        self.assertNotIn("sms_code", row)


if __name__ == "__main__":
    unittest.main()
