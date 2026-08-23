import unittest
from unittest.mock import patch

from core import gcash_eligibility_service as service
from webui.app import create_app


class GcashEligibilityServiceTests(unittest.TestCase):
    @staticmethod
    def _gcash_passed_account(account_id=7):
        return {
            "id": account_id,
            "email": "a@example.com",
            "access_token": "AT",
            "plan_type": "free",
            "plus_trial_eligible": False,
            "gcash_eligibility_status": "success",
            "gcash_eligibility_ok": True,
            "gcash_eligibility_outcome": "eligible",
            "gcash_eligibility_valid": True,
            "gcash_eligibility_payment_method_available": True,
            "gcash_eligibility_checkout_amount_is_zero": True,
            "gcash_eligibility_eligible": True,
        }

    def test_business_success_requires_all_eligibility_fields(self):
        payload = {
            "valid": True,
            "payment_method_available": True,
            "checkout_amount_is_zero": True,
            "eligible": True,
            "outcome": "eligible",
            "message": "$0 eligible · 0.00 PHP",
            "eligibility_proof": "secret-proof",
        }
        with patch.object(service, "_cdk", return_value="CDK"), \
             patch.object(service, "resolve_plan_check_route", return_value={"proxy": "", "network_route": "direct"}), \
             patch.object(service, "_request_once", return_value=(200, payload, {})):
            result = service.check_gcash_eligibility("AT")
        self.assertTrue(result["query_ok"])
        self.assertTrue(result["eligible"])
        self.assertTrue(result["qualification_ok"])
        self.assertEqual(result["outcome"], "eligible")
        self.assertNotIn("eligibility_proof", result)

    def test_http_200_ineligible_is_a_completed_query(self):
        payload = {
            "valid": True,
            "payment_method_available": False,
            "checkout_amount_is_zero": False,
            "eligible": False,
            "outcome": "ineligible",
            "reason": "gcash_payment_method_unavailable",
        }
        with patch.object(service, "_cdk", return_value="CDK"), \
             patch.object(service, "resolve_plan_check_route", return_value={"proxy": "", "network_route": "direct"}), \
             patch.object(service, "_request_once", return_value=(200, payload, {})):
            result = service.check_gcash_eligibility("AT")
        self.assertTrue(result["query_ok"])
        self.assertFalse(result["eligible"])
        self.assertFalse(result["qualification_ok"])
        self.assertEqual(result["reason"], "gcash_payment_method_unavailable")

    def test_malformed_200_response_is_not_reported_as_ineligible(self):
        with patch.object(service, "_cdk", return_value="CDK"), \
             patch.object(service, "resolve_plan_check_route", return_value={"proxy": "", "network_route": "direct"}), \
             patch.object(service, "_request_once", return_value=(200, {"message": "bad"}, {})):
            result = service.check_gcash_eligibility("AT")
        self.assertFalse(result["query_ok"])
        self.assertIn("必要业务字段", result["error"])

    def test_rate_limit_retries_with_retry_after(self):
        responses = [
            (429, {"detail": "zero_trial_rate_limited"}, {"Retry-After": "0"}),
            (200, {"valid": True, "payment_method_available": True, "checkout_amount_is_zero": True, "eligible": True, "outcome": "eligible"}, {}),
        ]
        with patch.object(service, "_cdk", return_value="CDK"), \
             patch.object(service, "resolve_plan_check_route", return_value={"proxy": "", "network_route": "direct"}), \
             patch.object(service, "_request_once", side_effect=responses), \
             patch.object(service.time, "sleep"):
            result = service.check_gcash_eligibility("AT")
        self.assertTrue(result["query_ok"])
        self.assertTrue(result["eligible"])

    def test_webui_route_queues_single_account(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        account = {"id": 7, "email": "a@example.com", "access_token": "AT"}
        with patch("webui.app.db.get_account", return_value=account), \
             patch("webui.app.gcash_eligibility_service.enqueue_account_gcash_eligibility", return_value={"accepted": True, "status": "queued", "account_id": 7}):
            response = client.post("/api/accounts/check-gcash-eligibility", json={"account_id": 7}, headers={"X-Auth-Code": "test-auth"})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["ok"])

    def test_gcash_qualification_unlocks_gcash_extract(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        account = self._gcash_passed_account()
        queued = {"accepted": True, "busy": False, "status": "queued", "account_id": 7}
        with patch("webui.app.db.get_account", return_value=account), \
             patch("webui.app.extract_link_service.enqueue_account_extract", return_value=queued) as enqueue:
            response = client.post(
                "/api/accounts/extract-link",
                json={"account_id": 7, "link_type": "gcash"},
                headers={"X-Auth-Code": "test-auth"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(enqueue.call_args.kwargs["link_type"], "gcash")

    def test_gcash_qualification_does_not_unlock_other_extract_types(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        account = self._gcash_passed_account()
        with patch("webui.app.db.get_account", return_value=account), \
             patch("webui.app.extract_link_service.enqueue_account_extract") as enqueue:
            response = client.post(
                "/api/accounts/extract-link",
                json={"account_id": 7, "link_type": "ideal"},
                headers={"X-Auth-Code": "test-auth"},
            )
        self.assertEqual(response.status_code, 400)
        enqueue.assert_not_called()

    def test_non_gcash_extract_keeps_plus_trial_rule(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        account = self._gcash_passed_account()
        account.update({
            "plus_trial_eligible": True,
            "gcash_eligibility_status": "success",
            "gcash_eligibility_ok": False,
            "gcash_eligibility_outcome": "ineligible",
        })
        queued = {"accepted": True, "busy": False, "status": "queued", "account_id": 7}
        with patch("webui.app.db.get_account", return_value=account), \
             patch("webui.app.extract_link_service.enqueue_account_extract", return_value=queued) as enqueue:
            response = client.post(
                "/api/accounts/extract-link",
                json={"account_id": 7, "link_type": "ideal"},
                headers={"X-Auth-Code": "test-auth"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(enqueue.call_args.kwargs["link_type"], "ideal")

    def test_default_extract_type_and_gate_use_same_resolved_value(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        account = self._gcash_passed_account()
        queued = {"accepted": True, "busy": False, "status": "queued", "account_id": 7}
        with patch("webui.app.db.get_account", return_value=account), \
             patch("webui.app.extract_link_service.resolve_link_type", return_value="gcash"), \
             patch("webui.app.extract_link_service.enqueue_account_extract", return_value=queued) as enqueue:
            response = client.post(
                "/api/accounts/extract-link",
                json={"account_id": 7},
                headers={"X-Auth-Code": "test-auth"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(enqueue.call_args.kwargs["link_type"], "gcash")

    def test_bulk_gcash_extract_accepts_qualified_account(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        passed = self._gcash_passed_account(7)
        rejected = {
            "id": 8,
            "email": "b@example.com",
            "access_token": "AT2",
            "plan_type": "free",
            "plus_trial_eligible": False,
            "gcash_eligibility_status": "success",
            "gcash_eligibility_ok": False,
            "gcash_eligibility_outcome": "ineligible",
        }
        queued = {"accepted": True, "busy": False, "status": "queued"}
        with patch("webui.app.db.get_account", side_effect=lambda account_id: passed if account_id == 7 else rejected), \
             patch("webui.app.extract_link_service.enqueue_account_extract", return_value=queued) as enqueue:
            response = client.post(
                "/api/accounts/extract-link-bulk",
                json={"account_ids": [7, 8], "link_type": "gcash"},
                headers={"X-Auth-Code": "test-auth"},
            )
        body = response.get_json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(body["started_count"], 1)
        self.assertEqual(body["skipped_count"], 1)
        self.assertEqual(enqueue.call_args.kwargs["account_id"], 7)


if __name__ == "__main__":
    unittest.main()
