import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import checkout_quotes as cq
from core import plan_check_service as service
from core import chatgpt_plan as chatgpt_plan
from requests.cookies import RequestsCookieJar
from types import SimpleNamespace


def config(*countries):
    return cq.CheckoutConfig({"entry_point": "all_plans_pricing_modal", "plan_name": "chatgptplusplan",
        "checkout_ui_mode": "custom", "promo_campaign": {"promo_campaign_id": "plus-1-month-free",
        "is_coupon_from_query_param": False}}, tuple(cq.CountryPlan(c, cur) for c, cur in countries), False)


def response(country="IN", currency="INR", amount=199900):
    return {"status": "open", "payment_status": "unpaid", "payment_method_types": ["card", "upi"],
            "custom_payment_methods": [], "billing_details": {"country": country, "currency": currency},
            "checkout_state": {"currency": currency.lower(), "canConfirm": False,
                "total": {"subtotal": {"minorUnitsAmount": 999999}, "total": {"minorUnitsAmount": amount}}}}


class CheckoutQuotesTests(unittest.TestCase):
    def setUp(self):
        cq._ROUND_ROBIN.clear()
        self.enterContext(patch.object(chatgpt_plan, "_plan_check_settings", return_value=(15, 3, 0)))

    def test_bodies_use_yaml_country_without_proxy_fields(self):
        c = config(("IN", "INR"), ("PH", "PHP"), ("BR", "BRL"), ("VN", "VND"))
        for p in c.plans:
            body = cq.build_body(c, p)
            self.assertEqual(body["billing_details"], {"country": p.country, "currency": p.currency})
            self.assertNotIn("proxy", body)
            self.assertEqual(body["promo_campaign"], {
                "promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False})
            self.assertNotIn("checkout_ui_mode", body)
            self.assertEqual(set(body), {"entry_point", "plan_name", "promo_campaign", "billing_details"})
        self.assertEqual(c.common["promo_campaign"]["promo_campaign_id"], "plus-1-month-free")

    def test_stripe_init_form_and_quote(self):
        checkout = {"checkout_provider":"stripe", "checkout_session_id":"cs_test_123",
            "publishable_key":"pk_test_123", "billing_details":{"country":"IN", "currency":"INR"}}
        form = cq._stripe_init_form("pk_test_123", stripe_js_id="stable-test-id")
        self.assertEqual(form["elements_session_client[elements_init_source]"], "custom_checkout")
        self.assertEqual(form["elements_session_client[stripe_js_id]"], "stable-test-id")
        self.assertEqual(form["key"], "pk_test_123")
        self.assertEqual(form["_stripe_version"], cq.STRIPE_API_VERSION)
        fake = Mock()
        fake.post.return_value.status_code = 200
        fake.post.return_value.json.return_value = {
            "object":"checkout.session", "status":"open", "currency":"inr",
            "invoice":{"amount_due":0,"currency":"inr"},
            "payment_method_types":["card","link","upi"],
        }
        result = cq._init_stripe_checkout(checkout,cq.CountryPlan("IN","INR"),None,
            "http://proxy.example:1000",15,http_factory=Mock(return_value=fake))
        self.assertEqual(cq.format_note([result]), "IN:0INR:[card,link,UPI]")
        self.assertEqual(fake.proxies["https"], "http://proxy.example:1000")
        self.assertFalse(fake.trust_env)
        self.assertEqual(fake.post.call_count,1)
        self.assertIn("cs_test_123/init",fake.post.call_args.args[0])
        self.assertEqual(fake.post.call_args.kwargs["headers"]["Origin"], "https://js.stripe.com")
        self.assertEqual(fake.post.call_args.kwargs["data"]["key"], "pk_test_123")
        fake.close.assert_called_once()

    def test_stripe_init_rejects_missing_amount_and_currency_mismatch(self):
        checkout = {"checkout_provider":"stripe", "billing_details":{"country":"IN", "currency":"INR"}}
        base = {"object":"checkout.session", "status":"open", "currency":"inr",
                "invoice":{"amount_due":None,"currency":"inr"}, "payment_method_types":["card"]}
        with self.assertRaises(cq.CheckoutError):
            cq.parse_stripe_init(checkout,base,cq.CountryPlan("IN","INR"))
        base["invoice"] = {"amount_due":100,"currency":"php"}
        with self.assertRaises(cq.CheckoutError):
            cq.parse_stripe_init(checkout,base,cq.CountryPlan("IN","INR"))

    def test_stripe_init_retries_transient_response_using_same_session(self):
        checkout = {"checkout_provider": "stripe", "checkout_session_id": "cs_test_123",
                    "publishable_key": "pk_test_123", "billing_details": {"country": "IN", "currency": "INR"}}
        fake = Mock()
        ok = Mock(status_code=200)
        ok.json.return_value = {"object": "checkout.session", "status": "open", "currency": "inr",
                                "invoice": {"amount_due": 0, "currency": "inr"},
                                "payment_method_types": ["card"]}
        fake.post.side_effect = [Mock(status_code=503), ok]
        with patch.object(cq.time, "sleep"):
            result = cq._init_stripe_checkout(checkout, cq.CountryPlan("IN", "INR"), None,
                                               "", 15, http_factory=Mock(return_value=fake))
        self.assertEqual(result["status"], "success")
        self.assertEqual(fake.post.call_count, 2)
        fake.close.assert_called_once()

    def test_exact_note_format_and_currency_precision(self):
        p = cq.CountryPlan("IN", "INR")
        for minor, expected in ((199900, "1999"), (1990, "19.9"), (0, "0")):
            quote = cq.parse_quote(response(amount=minor), p)
            self.assertEqual(cq.format_note([quote]), f"IN:{expected}INR:[card,UPI]")
        quote = cq.parse_quote(response("VN", "VND", 1999), cq.CountryPlan("VN", "VND"))
        self.assertEqual(quote["amount_display"], "1999")
        self.assertEqual(quote["amount_minor"], 1999)

    def test_missing_amount_never_means_free_and_methods_unknown_is_explicit(self):
        value = response(amount=None)
        quote = cq.parse_quote(value, cq.CountryPlan("IN", "INR"))
        self.assertIsNone(quote["amount_display"])
        self.assertNotIn(":0INR", cq.format_note([quote]))
        value = response()
        value["custom_payment_methods"] = [{"type": "pix"}, {"unexpected": "shape"}]
        quote = cq.parse_quote(value, cq.CountryPlan("IN", "INR"))
        self.assertEqual(quote["status"], "partial")
        self.assertIn("pix", quote["payment_method_types"])
        self.assertEqual(cq.format_note([quote]), "IN:1999INR:[card,UPI,pix]")
        self.assertTrue(quote["payment_methods_unknown"])

    def test_supported_checkout_data_wrapper(self):
        self.assertEqual(cq.parse_quote({"data": response()}, cq.CountryPlan("IN", "INR"))["amount_display"], "1999")

    def test_response_mismatch_is_not_accepted(self):
        with self.assertRaises(cq.CheckoutError):
            cq.parse_quote(response(currency="PHP"), cq.CountryPlan("IN", "INR"))

    def test_server_precision_and_har_japan_result(self):
        pricing = {"country_code": "JP", "currency_config": {"symbol_code": "JPY", "minor_unit_exponent": 0}}
        payload = response("JP", "JPY", 0)
        payload["payment_method_types"] = ["card", "link"]
        payload["checkout_state"]["total"]["subtotal"]["minorUnitsAmount"] = 2727
        payload["checkout_state"]["total"]["discount"] = {"minorUnitsAmount": 2727, "amount": "¥2,727"}
        self.assertEqual(cq.format_note([cq.parse_quote(payload, cq.CountryPlan("JP", "JPY"), pricing)]), "JP:0JPY:[card,link]")

    def test_proxy_rotation_is_country_scoped(self):
        p = cq.CountryPlan("IN", "INR", ("user:pwd@one.example:1000", "user:pwd@two.example:1000"))
        c = cq.CheckoutConfig({}, (p,), True)
        self.assertIn("one.example", cq.select_proxy(c,p))
        self.assertIn("two.example", cq.select_proxy(c,p))
        self.assertIn("one.example", cq.select_proxy(c,p))
        with self.assertRaises(cq.CheckoutError):
            cq.select_proxy(c,cq.CountryPlan("PH","PHP"))
        self.assertEqual(cq.select_proxy(config(("IN","INR")),p), "")

    def test_config_snapshot_and_errors_do_not_leak_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"checkout.yaml"
            path.write_text("plans: [broken-secret-proxy", encoding="utf-8")
            with self.assertRaises(cq.CheckoutError) as error:
                cq.load_config(path)
            self.assertNotIn("broken-secret", str(error.exception))
            path.write_text(Path("checkout.example.yaml").read_text(), encoding="utf-8")
            c = cq.load_config(path)
            self.assertEqual([p.country for p in c.plans], ["IN","PH","BR","VN"])
            self.assertNotIn("proxies", str(c.public_summary()))

    def test_country_post_once_on_timeout(self):
        session = Mock()
        session.get_chatgpt_headers.return_value = {}
        session._get_common_headers.return_value = {}
        session.get_chatgpt_navigate_headers.return_value = {}
        session.get.return_value = Mock(status_code=200)
        session.get.return_value.json.return_value = {}
        session.post.side_effect = TimeoutError("secret-proxy-url")
        c = config(("IN","INR"))
        with patch("core.openai_auth.request_sentinel_token", return_value={"token":"sentinel"}) as sentinel_req, \
             patch("core.openai_auth.build_sentinel_header", return_value=("sentinel-header", "")):
            result = cq.query_country(c,c.plans[0],"token",session_factory=Mock(return_value=session))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(session.post.call_count,1)
        self.assertNotIn("secret-proxy",str(result))

    def test_country_get_retries_403_in_same_session(self):
        session = Mock()
        session.get_chatgpt_headers.return_value = {}
        session._get_common_headers.return_value = {}
        session.get_chatgpt_navigate_headers.return_value = {}
        ok = Mock(status_code=200)
        ok.json.return_value = {}
        session.get.side_effect = [Mock(status_code=403), ok, ok, ok, ok, ok]
        checkout = Mock(status_code=200)
        checkout.json.return_value = response()
        session.post.return_value = checkout
        c = config(("IN", "INR"))
        with patch("core.openai_auth.request_sentinel_token", return_value={"token": "sentinel"}), \
             patch("core.openai_auth.build_sentinel_header", return_value=("sentinel-header", "")), \
             patch.object(cq.time, "sleep"):
            result = cq.query_country(c, c.plans[0], "AT", session_factory=Mock(return_value=session))
        self.assertEqual(result["status"], "success")
        self.assertEqual(session.get.call_count, 6)
        session.reset_circuit_breaker.assert_called_once()

    def test_country_post_retries_explicit_403_with_new_sentinel(self):
        session = Mock()
        session.get_chatgpt_headers.return_value = {}
        session._get_common_headers.return_value = {}
        session.get_chatgpt_navigate_headers.return_value = {}
        ok = Mock(status_code=200)
        ok.json.return_value = {}
        session.get.return_value = ok
        checkout = Mock(status_code=200)
        checkout.json.return_value = response()
        session.post.side_effect = [Mock(status_code=403), checkout]
        c = config(("IN", "INR"))
        with patch("core.openai_auth.request_sentinel_token", return_value={"token": "sentinel"}) as sentinel, \
             patch("core.openai_auth.build_sentinel_header", return_value=("sentinel-header", "")), \
             patch.object(cq.time, "sleep"):
            result = cq.query_country(c, c.plans[0], "AT", session_factory=Mock(return_value=session))
        self.assertEqual(result["status"], "success")
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(sentinel.call_count, 2)
        session.reset_circuit_breaker.assert_called_once()

    def test_country_reuses_plan_cookie_token_and_identity_with_own_proxy(self):
        jar = RequestsCookieJar()
        jar.set("session-cookie", "secret-value", domain=".chatgpt.com", path="/", secure=True)
        jar.set("unrelated", "never-send", domain="other.example", path="/")
        original = SimpleNamespace(device_id="saved-device", oai_session_id="saved-session",
            browser_profile={"user_agent": "saved-UA"}, session=SimpleNamespace(cookies=jar),
            get_chatgpt_headers=lambda **kw: {"User-Agent": "saved-UA", "oai-device-id": "saved-device",
                                           "oai-session-id": "saved-session"})
        # requests exposes a CookieJar directly; curl_cffi exposes it via .jar.
        original.session.cookies = SimpleNamespace(jar=jar)
        context = cq.capture_plan_context(original, "Bearer saved-token", {"account_id": "saved-account"})
        self.assertEqual(len(context.cookies), 1)
        self.assertNotIn("secret-value", repr(context))
        fake = Mock()
        target_jar = RequestsCookieJar()
        fake.session.cookies = SimpleNamespace(jar=target_jar, clear=target_jar.clear)
        fake._get_common_headers.return_value = {"User-Agent": "saved-UA"}
        fake.get.return_value = Mock(status_code=200)
        fake.get.return_value.json.return_value = {}
        fake.post.return_value = Mock(status_code=200)
        fake.post.return_value.json.return_value = response()
        plan = cq.CountryPlan("IN", "INR", ("user:pwd@india.example:1000",))
        c = cq.CheckoutConfig(config(("IN", "INR")).common, (plan,), True)
        factory = Mock(return_value=fake)
        with patch("core.openai_auth.request_sentinel_token", return_value={"token":"sentinel"}) as sentinel_req, \
             patch("core.openai_auth.build_sentinel_header", return_value=("sentinel-header", "")):
            result = cq.query_country(c,plan,"wrong-token",auth_context=context,session_factory=factory)
        self.assertEqual(result["status"],"success")
        self.assertEqual(factory.call_args.kwargs["device_id"],"saved-device")
        self.assertIn("india.example", factory.call_args.kwargs["proxy"])
        self.assertFalse(factory.call_args.kwargs["detect_exit_geo"])
        self.assertEqual(target_jar.get("session-cookie"),"secret-value")
        self.assertEqual(fake.get.call_count,4)  # main SDK, coupon check, app-store retry, pricing
        self.assertIn('/backend-api/sentinel/sdk.js', fake.get.call_args_list[0].args[0])
        self.assertIn('promo_campaign=plus-1-month-free', fake.get.call_args_list[0].kwargs['headers']['referer'])
        self.assertIn('/backend-api/promo_campaign/check_coupon', fake.get.call_args_list[1].args[0])
        self.assertEqual(fake.get.call_args_list[2].args[0],
                         'https://chatgpt.com/backend-api/subscriptions/has_app_store_subscription_in_billing_retry')
        self.assertEqual(fake.get.call_args_list[3].kwargs['headers']['x-openai-target-route'],
                         '/backend-api/checkout_pricing_config/configs/{country_code}')
        headers = fake.post.call_args.kwargs["headers"]
        self.assertEqual(headers["authorization"],"Bearer saved-token")
        self.assertEqual(headers["chatgpt-account-id"],"saved-account")
        self.assertEqual(headers["x-openai-target-path"],cq.CHECKOUT_PATH)
        self.assertEqual(headers["content-type"],"application/json")
        self.assertEqual(headers["openai-sentinel-token"],"sentinel-header")
        self.assertEqual(sentinel_req.call_args.args[1],"chatgpt_checkout")
        target_jar.set("session-cookie","changed",domain=".chatgpt.com",path="/")
        self.assertEqual(context.cookies[0].value,"secret-value")

    def test_checkout_sentinel_uses_chatgpt_origin_flow_and_page_context(self):
        from core import openai_auth
        from urllib.parse import urlsplit
        class Resp:
            status_code = 200
            def json(self):
                return {"persona":"chatgpt-noauth", "token":"challenge", "turnstile":{"required":False},
                        "proofofwork":{"required":False}, "so":{"required":False}}
            def raise_for_status(self): pass
        session = Mock()
        session.device_id = 'device-test'
        session.sentinel_sid = 'sid-test'
        session.browser_profile = {}
        session._get_common_headers.return_value = {"User-Agent":"test-agent"}
        session.get_chatgpt_navigate_headers.return_value = {"sec-fetch-dest":"document"}
        session.get.return_value = Resp()
        session.post.return_value = Resp()
        with patch.object(openai_auth, '_request_with_proxy_retry', side_effect=lambda _s,_label,fn:fn()), \
             patch.object(openai_auth, 'generate_requirements_token', return_value='proof-p'), \
             patch.object(openai_auth, 'build_sentinel_request_body', side_effect=lambda p,d,f:json.dumps({'p':p,'id':d,'flow':f})):
            result = openai_auth.request_sentinel_token(session, 'chatgpt_checkout',
                sentinel_origin='https://chatgpt.com', page_url='https://chatgpt.com/?promo_campaign=test')
        urls = [call.args[0] for call in session.get.call_args_list]
        self.assertEqual([urlsplit(u).path for u in urls], [
            '/sentinel/' + __import__('config').SENTINEL_SV + '/sdk.js',
            '/backend-api/sentinel/frame.html', '/sentinel/' + __import__('config').SENTINEL_SV + '/sdk.js'])
        self.assertEqual(session.post.call_count, 2)  # sentinel/req and matching frontend ping
        req = session.post.call_args_list[0]
        self.assertEqual(req.args[0], 'https://chatgpt.com/backend-api/sentinel/req')
        self.assertEqual(json.loads(req.kwargs['data'])['flow'], 'chatgpt_checkout')
        self.assertEqual(req.kwargs['headers']['origin'], 'https://chatgpt.com')
        self.assertEqual(result['token'], 'challenge')

    def test_401_and_429_stop_remaining(self):
        for status in (401,429):
            session=Mock()
            session.get.return_value=Mock(status_code=status, text="secret response")
            c=config(("IN","INR"))
            result=cq.query_country(c,c.plans[0],"token",session_factory=Mock(return_value=session))
            self.assertTrue(result["stop_remaining"])
            session.post.assert_not_called()

    def test_qualification_runs_independently_of_plan_eligibility(self):
        with patch.object(service.db,"update_checkout_run",return_value=True) as save, \
             patch.object(service.db,"get_account",return_value={"access_token":"fake"}), \
             patch.object(service,"_wait_for_rate_slot"), patch.object(cq,"query_country",return_value={"country":"IN","status":"success"}) as query:
            service._run_checkout_stage(1,"run",config(("IN","INR")))
        query.assert_called_once()
        self.assertEqual(save.call_args.kwargs["status"],"success")

    def test_country_errors_continue_without_replacing_original_plan(self):
        c=config(("IN","INR"),("PH","PHP"))
        with patch.object(service.db,"update_checkout_run",return_value=True) as save, \
             patch.object(service.db,"get_account",return_value={"access_token":"fake"}), \
             patch.object(service.db,"update_account_plan_check") as original, \
             patch.object(service,"_wait_for_rate_slot"), patch.object(cq,"query_country",side_effect=[
                 {"country":"IN","status":"failed","error":"促销拒绝"},
                 {"country":"PH","status":"success","currency":"PHP","amount_display":"0","payment_method_types":["card"]}]) as query:
            service._run_checkout_stage(1,"run",c)
        self.assertEqual(query.call_count,2)
        self.assertEqual(save.call_args.kwargs["status"],"partial")
        original.assert_not_called()

    def test_plan_run_never_invokes_checkout(self):
        account_result = {"ok": True, "current_plan_type": "free", "plus_trial_eligible": True}
        with patch.object(service.db, "mark_account_plan_check_running", return_value=True), \
             patch.object(service.db, "update_account_plan_check", return_value=True), \
             patch.object(service.db, "update_plan_run_lifecycle", return_value=True), \
             patch.object(service, "check_account_plan", return_value=account_result), \
             patch.object(service, "_run_checkout_stage") as checkout, \
             patch.object(service._QUEUE_SLOTS, "release"), \
             patch.object(service, "_wait_for_rate_slot"):
            result = service._run_plan_check(account_id=1,email="test@example.com",access_token="AT",
                trigger="manual",proxy=None,timezone_offset_min="-")
        self.assertEqual(result, account_result)
        checkout.assert_not_called()

    def test_eligibility_run_uses_checkout_even_for_non_trial_account(self):
        config_value = config(("IN", "INR"))
        context = object()

        with patch.object(service.db, "mark_account_checkout_running", return_value=True), \
             patch.object(service.db, "update_checkout_run", return_value=True), \
             patch.object(service.db, "get_account", return_value={"checkout_status": "success"}), \
             patch.object(service.db, "update_account_plan_check") as update_plan, \
             patch.object(service, "check_account_plan") as check_plan, \
             patch.object(service, "prepare_checkout_auth_context", return_value=context) as prepare, \
             patch.object(service, "_run_checkout_stage") as checkout, \
             patch.object(service._QUEUE_SLOTS, "release"):
            result = service._run_checkout_only(account_id=1, access_token="AT",
                run_id="run", config=config_value)
        self.assertTrue(result["ok"])
        checkout.assert_called_once_with(1, "run", config_value, context)
        prepare.assert_called_once_with("AT")
        check_plan.assert_not_called()
        update_plan.assert_not_called()

    def test_checkout_context_bootstrap_does_not_query_plan_api(self):
        browser = Mock()
        browser.get.return_value = Mock(status_code=200)
        route = {"proxy": "", "upstream_proxy": ""}
        context = object()
        with patch.object(chatgpt_plan, "token_claims", return_value={"email": "a@example.test"}), \
             patch.object(chatgpt_plan, "resolve_plan_check_route", return_value=route), \
             patch.object(chatgpt_plan, "_plan_check_settings", return_value=(15, 1, 0)), \
             patch.object(chatgpt_plan, "open_plan_check_proxy", return_value=("", None)), \
             patch.object(chatgpt_plan, "BrowserSession", return_value=browser), \
             patch.object(chatgpt_plan, "check_account_plan") as check_plan, \
             patch.object(cq, "capture_plan_context", return_value=context) as capture, \
             patch.object(chatgpt_plan, "close_browser_session") as close:
            result = chatgpt_plan.prepare_checkout_auth_context("AT")
        self.assertIs(result, context)
        browser.get.assert_called_once()
        capture.assert_called_once_with(browser, "AT", {"email": "a@example.test"})
        close.assert_called_once_with(browser)
        check_plan.assert_not_called()

    def test_checkout_context_bootstrap_retries_403_without_changing_browser(self):
        browser = Mock()
        browser.get.side_effect = [Mock(status_code=403), Mock(status_code=200)]
        with patch.object(chatgpt_plan, "token_claims", return_value={"email": "a@example.test"}), \
             patch.object(chatgpt_plan, "resolve_plan_check_route", return_value={"proxy": ""}), \
             patch.object(chatgpt_plan, "open_plan_check_proxy", return_value=("", None)), \
             patch.object(chatgpt_plan, "BrowserSession", return_value=browser) as factory, \
             patch.object(cq, "capture_plan_context", return_value=object()), \
             patch.object(chatgpt_plan, "close_browser_session"), \
             patch.object(chatgpt_plan.time, "sleep"):
            chatgpt_plan.prepare_checkout_auth_context("AT")
        factory.assert_called_once()
        self.assertEqual(browser.get.call_count, 2)
        browser.reset_circuit_breaker.assert_called_once()


if __name__ == "__main__":
    unittest.main()
