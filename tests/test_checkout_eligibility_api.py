import unittest
from unittest.mock import patch

from core import db, plan_check_service
from webui.app import create_app


class CheckoutEligibilityApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_manual_eligibility_queues_checkout_without_touching_plan(self):
        account = {"id": 7, "email": "test@example.com", "access_token": "AT"}
        with patch.object(db, "get_account", return_value=account), \
             patch.object(plan_check_service, "enqueue_account_checkout_check",
                          return_value={"accepted": True, "status": "queued"}) as queue, \
             patch.object(plan_check_service, "enqueue_account_plan_check") as plan_queue:
            result = self.client.post("/api/accounts/check-eligibility", json={"account_id": 7})
        self.assertEqual(result.status_code, 202)
        queue.assert_called_once_with(account_id=7, access_token="AT")
        plan_queue.assert_not_called()
        self.assertEqual(self.client.post("/api/accounts/check-gcash-eligibility",
                                          json={"account_id": 7}).status_code, 404)

    def test_qualify_does_not_require_free_plan_or_trial(self):
        account = {"id": 7, "email": "test@example.com", "access_token": "AT",
                   "current_plan_type": "plus", "plus_trial_eligible": False}
        with patch.object(db, "get_account", return_value=account), \
             patch.object(plan_check_service, "enqueue_account_checkout_check",
                          return_value={"accepted": True}) as queue:
            result = self.client.post("/api/accounts/check-eligibility", json={"account_id": 7})
        self.assertEqual(result.status_code, 202)
        queue.assert_called_once()

    def test_ph_zero_quote_gcash_gate_ignores_retired_fields(self):
        row = {"gcash_eligibility_status": "success", "gcash_eligibility_ok": True}
        self.assertFalse(db._checkout_gcash_eligible(row))
        row["checkout_quotes"] = {"results": [{"country": "PH", "currency": "PHP",
            "status": "success", "amount_minor": 0, "payment_method_types": ["card", "gcash"]}]}
        self.assertTrue(db._checkout_gcash_eligible(row))
        row["checkout_results_stale"] = True
        self.assertFalse(db._checkout_gcash_eligible(row))


if __name__ == "__main__":
    unittest.main()
