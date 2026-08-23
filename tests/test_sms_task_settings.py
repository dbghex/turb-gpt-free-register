# -*- coding: utf-8 -*-
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from config import codex as codex_config
from core import db, registration_service, sms_provider
from core.browser_use_codex_oauth import _run_in_isolated_thread


def _settings(provider="hero", **overrides):
    values = {
        "provider": provider,
        "service": "dr",
        "country": "187",
        "max_price": "0.50",
        "max_retries": 3,
        "code_wait": 90,
        "poll_interval": 3,
        "request_timeout": 20,
        "api_base": f"https://{provider}.example.test/api",
        "api_key": "top-secret",
    }
    values.update(overrides)
    return sms_provider.SmsTaskSettings(**values)


class SmsTaskSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_ledger = sms_provider._PENDING_CANCELLATIONS_PATH
        self.old_resumed = sms_provider._PENDING_RESUMED
        sms_provider._PENDING_CANCELLATIONS_PATH = Path(self.tmp.name) / "pending.json"
        sms_provider._PENDING_RESUMED = True

    def tearDown(self):
        sms_provider._PENDING_CANCELLATIONS_PATH = self.old_ledger
        sms_provider._PENDING_RESUMED = self.old_resumed
        sms_provider._ACQUISITIONS.clear()
        sms_provider._ACQUIRED_AT.clear()
        self.tmp.cleanup()

    def test_settings_are_frozen_and_public_snapshot_has_no_secret(self):
        settings = _settings(
            api_base="https://user:pass@hero.test/api?api_key=url-secret&lang=cn"
        )
        with self.assertRaises(FrozenInstanceError):
            settings.country = "10"
        snapshot = settings.public_snapshot()
        self.assertEqual(snapshot["provider"], "hero")
        self.assertTrue(snapshot["api_key_configured"])
        self.assertNotIn("api_key", snapshot)
        self.assertNotIn("top-secret", str(snapshot))
        self.assertNotIn("url-secret", str(snapshot))
        self.assertNotIn("user:pass", str(snapshot))

    def test_quote_buffer_locks_rounded_up_price(self):
        settings = _settings(
            max_price="",
            price_mode="quote_buffer",
            price_buffer_percent=15,
        )

        class Hero:
            def get_prices(self, service, country):
                return [
                    {"cost": 0.2, "count": 2, "physicalCount": 2},
                    {"cost": 0.3, "count": 5, "physicalCount": 5},
                ]

            def close(self):
                pass

        with patch("core.sms_provider._hero_client", return_value=Hero()):
            prepared = sms_provider.prepare_task_settings(settings)
        self.assertEqual(prepared.max_price, "0.2300")
        self.assertEqual(settings.max_price, "")

    def test_quote_buffer_refreshes_stale_ui_price_once(self):
        settings = _settings(
            max_price="9.99",
            price_mode="quote_buffer",
            price_buffer_percent=15,
            price_resolved=False,
        )

        class Hero:
            def get_prices(self, service, country):
                return [{"cost": 0.2, "count": 2, "physicalCount": 2}]

            def close(self):
                pass

        with patch("core.sms_provider._hero_client", return_value=Hero()) as factory:
            prepared = sms_provider.prepare_task_settings(settings)
            prepared_again = sms_provider.prepare_task_settings(prepared)

        self.assertEqual(prepared.max_price, "0.2300")
        self.assertTrue(prepared.price_resolved)
        self.assertEqual(prepared_again.max_price, "0.2300")
        factory.assert_called_once()

    def test_manual_price_requires_positive_value(self):
        with self.assertRaises(sms_provider.SmsConfigurationError):
            sms_provider.prepare_task_settings(
                _settings(max_price="", price_mode="manual")
            )

    def test_unlimited_price_clears_stale_cap_and_fixed_price(self):
        prepared = sms_provider.prepare_task_settings(
            _settings(max_price="9.99", price_mode="unlimited", fixed_price=True)
        )
        self.assertEqual(prepared.max_price, "")
        self.assertFalse(prepared.fixed_price)

    def test_context_binding_survives_runtime_hot_change(self):
        original = _settings(provider="l", api_base="http://l-before.test", api_key="adm")
        with sms_provider.bind_settings(original):
            with patch.object(codex_config, "SMS_PROVIDER", "h"):
                self.assertIs(sms_provider.current_settings(), original)
                self.assertEqual(sms_provider._provider(), "l")

    def test_activation_keeps_original_provider_without_context(self):
        original = _settings(provider="hero")
        sms_provider._remember_acquisition("activation-1", original)
        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"):
            resolved = sms_provider._settings_for_activation("activation-1")
        self.assertIs(resolved, original)

    def test_l_and_h_status_one_are_local_noops(self):
        class NoNetwork:
            def get(self, *args, **kwargs):
                raise AssertionError("status=1 must not call remote handler")

        for provider in ("l", "h"):
            settings = _settings(provider=provider, api_base="http://localhost:8788", api_key="adm")
            self.assertEqual(
                sms_provider.set_status("id-1", 1, http=NoNetwork(), settings=settings),
                "OK",
            )

    def test_browser_use_isolated_thread_copies_sms_context(self):
        settings = _settings(provider="hero")
        with sms_provider.bind_settings(settings):
            provider = _run_in_isolated_thread(
                lambda: sms_provider.current_settings().provider
            )
        self.assertEqual(provider, "hero")

    def test_hero_activation_uses_original_client_after_hot_change(self):
        settings = _settings(provider="hero")

        class Hero:
            def __init__(self):
                self.statuses = []

            def get_number(self, *args, **kwargs):
                return "hero-activation", "+1 (202) 555-0199"

            def get_status(self, activation_id):
                return "STATUS_OK:654321"

            def set_status(self, activation_id, status):
                self.statuses.append((activation_id, status))
                return "ACCESS_ACTIVATION"

        hero = Hero()
        with patch("core.sms_provider._hero_client", return_value=hero):
            with sms_provider.bind_settings(settings):
                activation_id, phone = sms_provider.acquire_number(http=object())
            with patch.object(codex_config, "SMS_PROVIDER", "grizzly"):
                self.assertEqual(
                    sms_provider.wait_for_sms_code(
                        activation_id, http=object(), max_wait=1, poll_interval=0
                    ),
                    "654321",
                )
                sms_provider.complete(activation_id, http=object())

        self.assertEqual(phone, "12025550199")
        self.assertEqual(hero.statuses, [("hero-activation", 6)])

    def test_background_cancel_captures_original_settings(self):
        settings = _settings(provider="hero")
        sms_provider._remember_acquisition("hero-cancel", settings)
        captured = {}

        class Thread:
            def __init__(self, *, target, args, name, daemon):
                captured.update(target=target, args=args, name=name, daemon=daemon)

            def start(self):
                captured["started"] = True

        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), patch.object(
            sms_provider.threading, "Thread", Thread
        ):
            sms_provider.cancel("hero-cancel")

        self.assertTrue(captured["started"])
        self.assertIs(captured["args"][1], settings)

    def test_complete_failure_still_removes_pending_cancel_record(self):
        settings = _settings(provider="hero")
        sms_provider._remember_acquisition("hero-complete", settings)
        with patch(
            "core.sms_provider.set_status",
            side_effect=sms_provider.SmsRetryableProviderError("temporary"),
        ):
            sms_provider.complete("hero-complete", http=object(), settings=settings)
        self.assertNotIn(("hero", "hero-complete"), sms_provider._ACQUISITIONS)
        self.assertEqual(sms_provider._ledger_read(), [])

    def test_db_snapshot_sanitizer_drops_accidental_secrets(self):
        sanitized = db._sanitize_sms_snapshot({
            "provider": "hero",
            "api_key": "secret-value",
            "H_ADMIN_AUTH_CODE": "admin-value",
            "api_key_configured": True,
        })
        self.assertEqual(sanitized, {"provider": "hero", "api_key_configured": True})

    def test_submit_validates_sms_before_consuming_proxy(self):
        error = sms_provider.SmsConfigurationError("missing service")
        with patch(
            "core.registration_service.sms_provider.prepare_task_settings",
            side_effect=error,
        ), patch(
            "config.proxy.take_registration_proxies"
        ) as take_proxies, patch(
            "config.roxybrowser.REGISTRATION_DRIVER", "protocol"
        ):
            with self.assertRaises(sms_provider.SmsConfigurationError):
                registration_service.submit_registration(count=1)
        take_proxies.assert_not_called()

    def test_submit_passes_full_settings_but_persists_public_snapshot(self):
        settings = _settings(provider="hero")
        submitted = []

        class Executor:
            def submit(self, *args):
                submitted.append(args)

        job = {"id": 7, "log_file": "/tmp/job-7.log", "status": "pending"}
        with patch(
            "core.registration_service.sms_provider.prepare_task_settings",
            return_value=settings,
        ), patch(
            "config.proxy.take_registration_proxies", return_value=["proxy-one"]
        ), patch(
            "config.roxybrowser.REGISTRATION_DRIVER", "protocol"
        ), patch(
            "core.registration_service.get_executor", return_value=Executor()
        ), patch(
            "core.registration_service.get_executor_workers", return_value=1
        ), patch(
            "core.registration_service.db.create_job", return_value=job
        ) as create_job, patch(
            "core.registration_service.db.get_job", return_value=job
        ):
            registration_service.submit_registration(
                count=1, email_source="generic_api", workers=1
            )

        persisted = create_job.call_args.kwargs["sms_snapshot"]
        self.assertNotIn("api_key", persisted)
        self.assertNotIn("top-secret", str(persisted))
        self.assertIs(submitted[0][-1], settings)


if __name__ == "__main__":
    unittest.main()
