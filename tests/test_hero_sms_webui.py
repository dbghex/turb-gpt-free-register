# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch
from urllib.parse import quote

from config import codex as codex_config
from config import env_loader
from core import sms_provider
from core.hero_sms_client import HeroSmsError
from webui import config_editor
from webui.app import _registration_submit_error, create_app


class HeroSmsConfigTests(unittest.TestCase):
    def test_config_fields_and_secret_registry_are_complete(self):
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        expected = {
            "HERO_SMS_API_BASE",
            "HERO_SMS_API_KEY",
            "HERO_SMS_SERVICE",
            "HERO_SMS_COUNTRY",
            "HERO_SMS_PRICE_MODE",
            "HERO_SMS_MAX_PRICE",
            "HERO_SMS_PRICE_BUFFER_PERCENT",
            "HERO_SMS_FIXED_PRICE",
            "HERO_SMS_OPERATOR",
            "HERO_SMS_PHONE_EXCEPTION",
        }

        self.assertTrue(expected.issubset(fields))
        self.assertIn("HERO_SMS_API_KEY", env_loader.SECRET_ENV_KEYS)
        self.assertTrue(fields["HERO_SMS_API_KEY"].get("secret"))
        self.assertTrue(fields["HERO_SMS_API_KEY"].get("write_only"))
        self.assertTrue(all(fields[key]["group"] == "接码平台" for key in expected))

    def test_get_config_only_reports_that_hero_key_is_configured(self):
        secret = "hero-key-that-must-not-be-returned"
        with patch("config.env_loader.load_env"), patch(
            "config.env_loader.read_env_file", return_value={"HERO_SMS_API_KEY": secret}
        ):
            fields = {field["key"]: field for field in config_editor.get_config()}

        hero_key = fields["HERO_SMS_API_KEY"]
        self.assertEqual(hero_key["value"], "")
        self.assertTrue(hero_key["configured"])
        self.assertNotIn(secret, repr(hero_key))

    @patch("config.env_loader.load_env")
    @patch("config.env_loader.write_env_values")
    def test_blank_hero_key_save_preserves_existing_value(self, write_env_values, load_env):
        result = config_editor.update_config({
            "HERO_SMS_API_KEY": "   ",
            "HERO_SMS_COUNTRY": "6",
        })

        self.assertIn("HERO_SMS_API_KEY", result["preserved"])
        self.assertNotIn("HERO_SMS_API_KEY", result["updated"])
        write_env_values.assert_called_once_with({"HERO_SMS_COUNTRY": "6"})
        load_env.assert_called_with(override=True)

    @patch("config.env_loader.load_env")
    @patch("config.env_loader.write_env_values")
    def test_hero_key_requires_explicit_clear(self, write_env_values, load_env):
        result = config_editor.update_config({
            "__clear_secrets__": ["HERO_SMS_API_KEY"],
        })

        self.assertIn("HERO_SMS_API_KEY", result["updated"])
        write_env_values.assert_called_once_with({"HERO_SMS_API_KEY": ""})
        load_env.assert_called_with(override=True)


class HeroSmsWebUiApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("core.hero_sms_client.HeroSmsClient")
    def test_read_only_endpoints_delegate_to_hero_client(self, client_class):
        client = client_class.return_value
        client.get_balance.return_value = 12.5
        client.get_countries.return_value = [{"id": 6, "eng": "Indonesia", "chn": "印度尼西亚"}]
        client.get_services.return_value = [{"code": "dr", "name": "OpenAI"}]
        client.get_prices.return_value = [{"cost": 0.25, "count": 4, "physicalCount": 3}]
        common = {
            "api_base": "https://hero.example.test/stubs/handler_api.php",
            "api_key": "temporary-key",
            "timeout": 17,
        }

        balance = self.client.post("/api/sms/hero/balance", json=common)
        countries = self.client.post("/api/sms/hero/countries", json=common)
        services = self.client.post(
            "/api/sms/hero/services",
            json={**common, "country": "6", "lang": "cn"},
        )
        prices = self.client.post(
            "/api/sms/hero/prices",
            json={**common, "country": "6", "service": "dr"},
        )

        self.assertEqual(balance.status_code, 200)
        self.assertEqual(balance.get_json(), {"ok": True, "balance": 12.5})
        self.assertEqual(countries.get_json()["countries"][0]["id"], 6)
        self.assertEqual(services.get_json()["services"][0]["code"], "dr")
        self.assertEqual(prices.get_json()["prices"][0]["cost"], 0.25)
        self.assertEqual(client_class.call_count, 4)
        for call in client_class.call_args_list:
            self.assertEqual(call.kwargs, {
                "api_base": common["api_base"],
                "api_key": common["api_key"],
                "timeout": 17,
            })
        self.assertEqual(client.close.call_count, 4)
        client.get_services.assert_called_once_with(country="6", lang="cn")
        client.get_prices.assert_called_once_with("dr", "6")

    @patch("core.hero_sms_client.HeroSmsClient")
    def test_blank_request_key_falls_back_to_saved_key(self, client_class):
        client_class.return_value.get_countries.return_value = []
        with patch.object(codex_config, "HERO_SMS_API_KEY", "saved-key"), patch.object(
            codex_config, "HERO_SMS_API_BASE", "https://saved.example.test/api"
        ), patch.object(codex_config, "SMS_REQUEST_TIMEOUT", 29):
            response = self.client.post(
                "/api/sms/hero/countries",
                json={"api_key": "", "api_base": ""},
            )

        self.assertEqual(response.status_code, 200)
        client_class.assert_called_once_with(
            api_base="https://saved.example.test/api",
            api_key="saved-key",
            timeout=29,
        )

    @patch("core.hero_sms_client.HeroSmsClient")
    def test_prices_requires_country_and_service_without_calling_hero(self, client_class):
        with patch.object(codex_config, "HERO_SMS_API_KEY", "saved-key"), patch.object(
            codex_config, "HERO_SMS_COUNTRY", ""
        ), patch.object(codex_config, "HERO_SMS_SERVICE", ""):
            response = self.client.post("/api/sms/hero/prices", json={})

        self.assertEqual(response.status_code, 400)
        self.assertIn("服务", response.get_json()["error"])
        client_class.assert_not_called()

    @patch("core.hero_sms_client.HeroSmsClient")
    def test_error_payload_never_echoes_hero_key(self, client_class):
        secret = "hero+/super?secret"
        client_class.return_value.get_balance.side_effect = HeroSmsError(
            f"BAD_KEY: https://hero.example.test/api?api_key={quote(secret, safe='')}",
            code="BAD_KEY",
            details=f"api_key={secret}",
            info={"api_key": secret, "request": f"?api_key={secret}"},
            http_status=401,
        )

        response = self.client.post(
            "/api/sms/hero/balance",
            json={"api_key": secret, "api_base": "https://hero.example.test/api"},
        )

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(secret, body)
        self.assertEqual(response.get_json()["code"], "BAD_KEY")
        self.assertEqual(response.get_json()["info"]["api_key"], "[REDACTED]")

    def test_registration_preflight_errors_are_user_facing(self):
        app = create_app(auth_code="test-auth")
        with app.app_context():
            config_response, config_status = _registration_submit_error(
                sms_provider.SmsConfigurationError("未选择 HeroSMS 国家")
            )
            stock_response, stock_status = _registration_submit_error(
                sms_provider.SmsNoNumbersError("HeroSMS 当前没有库存")
            )

        self.assertEqual(config_status, 400)
        self.assertIn("国家", config_response.get_json()["error"])
        self.assertEqual(stock_status, 409)
        self.assertIn("库存", stock_response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
