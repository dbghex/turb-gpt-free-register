# -*- coding: utf-8 -*-
import json
import unittest

from core.hero_sms_client import (
    HeroSmsActivationError,
    HeroSmsAuthError,
    HeroSmsClient,
    HeroSmsLimitError,
    HeroSmsNoBalanceError,
    HeroSmsNoNumbersError,
    HeroSmsPurchaseUnknownError,
    HeroSmsServerError,
    HeroSmsValidationError,
    HeroSmsWrongMaxPriceError,
    redact_api_key,
)


class _Response:
    def __init__(self, value, status_code=200, *, encode_json=False):
        self.status_code = status_code
        self.text = json.dumps(value) if encode_json else str(value)


class _Http:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self):
        self.closed = True


def _client(*responses, key="hero-secret"):
    http = _Http(*responses)
    return HeroSmsClient("https://hero-sms.test/stubs/handler_api.php", key, timeout=7, http=http), http


class HeroSmsClientTests(unittest.TestCase):
    def test_get_balance_supports_plain_text_and_json_string(self):
        client, http = _client(_Response("ACCESS_BALANCE:12.50"), _Response("ACCESS_BALANCE:9.25", encode_json=True))

        self.assertEqual(client.get_balance(), 12.5)
        self.assertEqual(client.get_balance(), 9.25)
        self.assertEqual(http.calls[0]["params"], {"api_key": "hero-secret", "action": "getBalance"})
        self.assertEqual(http.calls[0]["timeout"], 7)

    def test_get_countries_supports_documented_list_and_keyed_object(self):
        documented = [{"id": 2, "rus": "Казахстан", "eng": "Kazakhstan", "chn": "哈萨克斯坦", "visible": 1, "retry": 1}]
        keyed = {"6": {"eng": "Indonesia", "chn": "印度尼西亚", "visible": "1", "retry": "0"}}
        client, _ = _client(_Response(documented, encode_json=True), _Response(keyed, encode_json=True))

        self.assertEqual(client.get_countries()[0]["id"], 2)
        second = client.get_countries()[0]
        self.assertEqual(second["id"], 6)
        self.assertEqual(second["visible"], 1)

    def test_get_services_normalizes_documented_and_keyed_shapes(self):
        documented = {"status": "success", "services": [{"code": "dr", "name": "OpenAI"}]}
        keyed = {"services": {"tg": "Telegram", "go": {"name": "Google"}}}
        client, http = _client(_Response(documented, encode_json=True), _Response(keyed, encode_json=True))

        self.assertEqual(client.get_services(country=6), [{"code": "dr", "name": "OpenAI"}])
        self.assertEqual(client.get_services(lang="en"), [{"code": "go", "name": "Google"}, {"code": "tg", "name": "Telegram"}])
        self.assertEqual(http.calls[0]["params"]["country"], "6")
        self.assertEqual(http.calls[0]["params"]["lang"], "cn")

    def test_get_prices_normalizes_schema_and_documented_example(self):
        schema_shape = {"6": {"dr": {"cost": 0.5, "count": 10, "physicalCount": 8}}}
        example_shape = [{"dr": {"cost": 0.08, "count": 25_370, "physicalCount": 14_528}}]
        client, _ = _client(_Response(schema_shape, encode_json=True), _Response(example_shape, encode_json=True))

        self.assertEqual(client.get_prices("dr", 6), [{
            "country": "6", "service": "dr", "cost": 0.5, "count": 10, "physicalCount": 8,
        }])
        self.assertEqual(client.get_prices("dr", 2), [{
            "country": "2", "service": "dr", "cost": 0.08, "count": 25370, "physicalCount": 14528,
        }])

    def test_get_prices_normalizes_wrapped_and_direct_quote_shapes(self):
        wrapped = {"status": "success", "prices": {"dr": {"price": "0.25", "quantity": "4"}}}
        direct = {"cost": 0.3, "count": 2, "physical_count": 1}
        client, _ = _client(_Response(wrapped, encode_json=True), _Response(direct, encode_json=True))

        self.assertEqual(client.get_prices("dr", 175)[0]["cost"], 0.25)
        self.assertEqual(client.get_prices("dr", 175)[0]["physicalCount"], 1)

    def test_get_number_builds_official_query_and_cleans_phone(self):
        client, http = _client(_Response("ACCESS_NUMBER:123456789:+1 (202) 555-0199"))

        result = client.get_number(
            "dr",
            12,
            max_price="0.5000",
            fixed_price=True,
            operator=["globe", "smart"],
            phone_exception="7900, 7934",
            ref="ref-1",
        )

        self.assertEqual(result, ("123456789", "12025550199"))
        self.assertEqual(http.calls[0]["params"], {
            "api_key": "hero-secret",
            "action": "getNumber",
            "service": "dr",
            "country": "12",
            "maxPrice": "0.5",
            "fixedPrice": "true",
            "operator": "globe,smart",
            "phoneException": "7900,7934",
            "ref": "ref-1",
        })

    def test_get_number_supports_json_object(self):
        client, _ = _client(_Response({"activationId": "act-2", "phoneNumber": "+639171234567"}, encode_json=True))
        self.assertEqual(client.get_number("dr", 12), ("act-2", "639171234567"))

    def test_get_number_v2_preserves_details_and_supports_provider_aliases(self):
        payload = {
            "activationId": 901,
            "phoneNumber": "+55 (91) 98013-3818",
            "activationCost": "0.1250",
            "activationOperator": "hero-br-1",
            "currency": "usd",
            "canGetAnotherSms": "1",
        }
        client, http = _client(_Response({"status": "success", "data": payload}, encode_json=True))

        result = client.get_number_v2(
            "ts",
            73,
            max_price="0.20",
            fixed_price=True,
            provider_id="hero-br-1",
            operator=["claro", "vivo"],
            phone_exception="1199,2198",
            ref="ba-task",
        )

        self.assertEqual(result["activationId"], "901")
        self.assertEqual(result["phoneNumber"], "5591980133818")
        self.assertEqual(result["activationCost"], 0.125)
        self.assertEqual(result["currency"], "USD")
        self.assertTrue(result["canGetAnotherSms"])
        self.assertEqual(http.calls[0]["params"], {
            "api_key": "hero-secret",
            "action": "getNumberV2",
            "service": "ts",
            "country": "73",
            "maxPrice": "0.2",
            "fixedPrice": "true",
            "operator": "claro,vivo",
            "phoneException": "1199,2198",
            "providerIds": "hero-br-1",
            "ref": "ba-task",
        })

    def test_get_number_v2_timeout_is_purchase_unknown(self):
        client, _ = _client(TimeoutError("getNumberV2 timed out"))
        with self.assertRaises(HeroSmsPurchaseUnknownError) as caught:
            client.get_number_detailed("ts", 73)
        self.assertEqual(caught.exception.code, "PURCHASE_RESULT_UNKNOWN")
        self.assertFalse(caught.exception.retryable)

    def test_status_lifecycle_supports_plain_and_json_string(self):
        client, http = _client(
            _Response("STATUS_WAIT_CODE"),
            _Response("STATUS_OK:605250", encode_json=True),
            _Response("ACCESS_RETRY_GET"),
            _Response("ACCESS_ACTIVATION"),
            _Response("ACCESS_CANCEL"),
        )

        self.assertEqual(client.get_status("act-1"), "STATUS_WAIT_CODE")
        self.assertEqual(client.get_status("act-1"), "STATUS_OK:605250")
        self.assertEqual(client.set_status("act-1", 3), "ACCESS_RETRY_GET")
        self.assertEqual(client.set_status("act-1", 6), "ACCESS_ACTIVATION")
        self.assertEqual(client.set_status("act-1", 8), "ACCESS_CANCEL")
        self.assertEqual(http.calls[-1]["params"]["status"], 8)

    def test_text_error_classification(self):
        cases = [
            ("NO_NUMBERS", HeroSmsNoNumbersError, True),
            ("NO_BALANCE", HeroSmsNoBalanceError, False),
            ("BAD_KEY", HeroSmsAuthError, False),
            ("BAD_SERVICE", HeroSmsValidationError, False),
            ("CHANNELS_LIMIT", HeroSmsLimitError, False),
            ("EARLY_CANCEL_DENIED", HeroSmsActivationError, False),
            ("ERROR_SQL", HeroSmsServerError, True),
        ]
        for body, error_type, retryable in cases:
            with self.subTest(body=body):
                client, _ = _client(_Response(body))
                with self.assertRaises(error_type) as caught:
                    client.get_balance()
                self.assertEqual(caught.exception.retryable, retryable)

    def test_json_error_classification_and_minimum_price(self):
        payload = {
            "title": "WRONG_MAX_PRICE",
            "details": "The maximum price is less than the permitted price",
            "info": {"min": 0.1234},
        }
        client, _ = _client(_Response(payload, status_code=400, encode_json=True))

        with self.assertRaises(HeroSmsWrongMaxPriceError) as caught:
            client.get_number("dr", 6, max_price=0.1)

        self.assertEqual(caught.exception.code, "WRONG_MAX_PRICE")
        self.assertEqual(caught.exception.minimum_price, 0.1234)
        self.assertEqual(caught.exception.info, {"min": 0.1234})

    def test_text_wrong_max_price_extracts_minimum(self):
        client, _ = _client(_Response("WRONG_MAX_PRICE:0.025"))
        with self.assertRaises(HeroSmsWrongMaxPriceError) as caught:
            client.get_number("dr", 6, max_price=0.01)
        self.assertEqual(caught.exception.minimum_price, 0.025)

    def test_false_status_object_is_validation_error(self):
        client, _ = _client(_Response({"status": "false", "msg": "country is incorrect"}, encode_json=True))
        with self.assertRaises(HeroSmsValidationError) as caught:
            client.get_prices("dr", 999)
        self.assertEqual(caught.exception.code, "WRONG_COUNTRY")

    def test_get_number_timeout_is_purchase_unknown_and_not_retryable(self):
        secret = "top-secret/key"
        timeout = TimeoutError(f"timed out: https://hero.test/?api_key={secret}&action=getNumber")
        client, _ = _client(timeout, key=secret)

        with self.assertRaises(HeroSmsPurchaseUnknownError) as caught:
            client.get_number("dr", 6)

        error = caught.exception
        self.assertEqual(error.code, "PURCHASE_RESULT_UNKNOWN")
        self.assertFalse(error.retryable)
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, error.details)
        self.assertIsNone(error.__cause__)

    def test_non_purchase_network_timeout_is_retryable_and_redacted(self):
        secret = "key-with+symbols"
        client, _ = _client(TimeoutError(f"timeout api_key={secret}"), key=secret)
        with self.assertRaisesRegex(Exception, "网络请求失败") as caught:
            client.get_balance()
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn(secret, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_json_error_fields_and_unknown_http_body_are_redacted(self):
        secret = "never-show-this"
        error_payload = {"title": "BANNED", "details": f"blocked api_key={secret}", "info": {"api_key": secret}}
        client, _ = _client(_Response(error_payload, status_code=403, encode_json=True), key=secret)
        with self.assertRaises(HeroSmsLimitError) as caught:
            client.get_balance()
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, caught.exception.details)
        self.assertEqual(caught.exception.info["api_key"], "***")

    def test_local_validation_does_not_make_request(self):
        client, http = _client()
        with self.assertRaises(HeroSmsValidationError):
            client.get_number("dr", 6, fixed_price=True)
        with self.assertRaises(HeroSmsValidationError):
            client.set_status("act", 1)
        with self.assertRaises(HeroSmsValidationError):
            client.get_prices("", 6)
        with self.assertRaises(HeroSmsValidationError):
            client.get_number("dr", 6, phone_exception=[str(i) for i in range(21)])
        self.assertEqual(http.calls, [])

    def test_missing_key_is_auth_error_without_request(self):
        client, http = _client(key="")
        with self.assertRaises(HeroSmsAuthError) as caught:
            client.get_balance()
        self.assertEqual(caught.exception.code, "NO_KEY")
        self.assertEqual(http.calls, [])

    def test_context_manager_only_closes_owned_session(self):
        external = _Http(_Response("ACCESS_BALANCE:1"))
        with HeroSmsClient("https://hero.test/api", "key", http=external) as client:
            self.assertEqual(client.get_balance(), 1.0)
        self.assertFalse(external.closed)

    def test_redact_api_key_handles_urls_and_encoded_values(self):
        secret = "a key/+"
        value = f"https://hero.test/?api_key={secret}&next=1; encoded={secret.replace(' ', '+')}"
        redacted = redact_api_key(value, secret)
        self.assertNotIn(secret, redacted)
        self.assertIn("api_key=***", redacted)


if __name__ == "__main__":
    unittest.main()
