"""Checkout state, notes and restart behavior against an isolated SQLite store."""
import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from core import db


class CheckoutPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        storage = {
            "_DATA_DIR": root,
            "_LOG_DIR": root / "logs",
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json",
            "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json",
            "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
            "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
            "_LEGACY_SQLITE": root / "legacy.db",
            "_CODEX_DIR": root / "codex",
            "_CODEX_AGENT_DIR": root / "agent",
            "_LEGACY_CODEX_EXPORT_STATE": root / "state.json",
            "_SQLITE_READY": False,
            "_SQLITE_READY_PATH": None,
        }
        self.enterContext(patch.multiple(db, **storage))
        # State transitions occur within one second: polling must still notice them.
        self.enterContext(patch.object(db, "_now", return_value=datetime.now().isoformat(timespec="seconds")))
        self.account_id = db.insert_account(email="checkout@example.test", access_token="test-token")
        self.assertEqual(db._active_sqlite_path().parent, root)

    @staticmethod
    def quote(country="IN", amount="0.00", currency="INR"):
        return {
            "country": country,
            "status": "success",
            "amount_minor": 0 if amount in {"0", "0.00"} else 199900,
            "amount_display": amount,
            "currency": currency,
            "payment_method_types": ["card"],
            "error": "",
        }

    def start(self, run_id="run-1", account_id=None):
        account_id = account_id or self.account_id
        self.assertTrue(db.claim_account_checkout(account_id, run_id=run_id))
        self.assertTrue(db.mark_account_checkout_running(account_id, run_id=run_id))
        self.assertTrue(db.update_account_plan_check(account_id, result={
            "ok": True, "current_plan_type": "free", "plus_trial_eligible": True,
            "plus_trial_campaign_id": "test-campaign", "http_status": 200,
        }, run_id=run_id))

    def save(self, results=None, *, account_id=None, run_id="run-1", finish=False, status="success"):
        self.assertTrue(db.update_checkout_run(
            account_id or self.account_id, run_id, status=status,
            results=results if results is not None else [self.quote()],
            config_summary={"plan_name": "chatgptplusplan"}, finish=finish,
        ))

    def test_existing_note_is_preserved_without_checkout_text(self):
        # Simulate a pre-feature row that only has the original note field.
        rows = db._load_accounts()
        rows[0]["note"] = "人工备注\n第二行"
        rows[0].pop("manual_note", None)
        db._save_accounts(rows)
        self.start()
        self.save(finish=True)
        row = db.get_account(self.account_id)
        self.assertEqual(row["manual_note"], "人工备注\n第二行")
        self.assertEqual(row["checkout_note"], "")
        self.assertEqual(row["note"], "人工备注\n第二行")
        self.assertEqual(row["checkout_zero_quotes"], [{"country": "IN", "currency": "INR", "payment_method_types": ["card"]}])

    def test_single_note_edit_and_clear_preserve_country_results(self):
        self.start()
        self.save(finish=True)
        saved_quotes = db.get_account(self.account_id)["checkout_quotes"]
        for note in ("客服备注", ""):
            with self.subTest(note=note):
                self.assertTrue(db.update_account_note(self.account_id, note))
                row = db.get_account(self.account_id)
                self.assertEqual(row["manual_note"], note)
                self.assertEqual(row["checkout_quotes"], saved_quotes)
                self.assertEqual(row["note"], note)
                self.assertEqual(len(row["checkout_zero_quotes"]), 1)

    def test_bulk_note_save_preserves_each_accounts_quote(self):
        other = db.insert_account(email="other@example.test", access_token="other-token")
        for account_id, quote in ((self.account_id, self.quote()), (other, self.quote("JP", "0", "JPY"))):
            self.start(account_id=account_id)
            self.save([quote], account_id=account_id, finish=True)
        for manual in ("批量备注", ""):
            updated, skipped = db.update_accounts_note([self.account_id, other, 99999], manual)
            self.assertEqual(len(updated), 2)
            self.assertEqual([item["id"] for item in skipped], [99999])
            for account_id in (self.account_id, other):
                row = db.get_account(account_id)
                self.assertEqual(row["manual_note"], manual)
                self.assertEqual(row["checkout_note"], "")
                self.assertEqual(row["note"], manual)
                self.assertEqual(len(row["checkout_zero_quotes"]), 1)

    def test_new_run_replaces_quotes_without_changing_note(self):
        db.update_account_note(self.account_id, "保留备注")
        self.start()
        self.save(finish=True)
        old = db.get_account(self.account_id)["checkout_quotes"]
        self.start("run-2")
        self.assertTrue(db.get_account(self.account_id)["checkout_results_stale"])
        self.save([self.quote("JP", "0", "JPY")], run_id="run-2", status="running")
        self.save([self.quote("JP", "0", "JPY")], run_id="run-2", finish=True)
        row = db.get_account(self.account_id)
        self.assertEqual(row["note"], "保留备注")
        self.assertEqual(row["checkout_zero_quotes"][0]["country"], "JP")
        self.assertEqual(row["checkout_previous_quotes"], old)
        self.assertFalse(row["checkout_results_stale"])

    def test_only_confirmed_zero_quotes_are_exposed(self):
        self.start()
        self.save([
            self.quote("IN", "0", "INR"),
            self.quote("VN", "1999", "VND"),
            {"country": "BR", "currency": "BRL", "status": "failed", "amount_minor": 0,
             "payment_method_types": ["pix"]},
            {"country": "PH", "currency": "PHP", "status": "success", "amount_minor": None,
             "payment_method_types": ["gcash"]},
        ], finish=True)
        row = db.get_account(self.account_id)
        self.assertEqual(row["checkout_zero_quotes"], [
            {"country": "IN", "currency": "INR", "payment_method_types": ["card"]}])
        self.assertEqual(row["note"], "")
        self.assertEqual(db.list_account_plan_check_statuses()["items"][0]["checkout_zero_quotes"], row["checkout_zero_quotes"])

    def test_legacy_generated_note_is_hidden_and_cleared_on_next_save(self):
        rows = db._load_accounts()
        rows[0].update(note="人工备注\n\nIN:0INR:[card]", checkout_note="IN:0INR:[card]")
        rows[0].pop("manual_note", None)
        db._save_accounts(rows)
        self.assertEqual(db.get_account(self.account_id)["note"], "人工备注")
        self.assertTrue(db.update_account_note(self.account_id, "新的人工备注"))
        row = db.get_account(self.account_id)
        self.assertEqual(row["note"], "新的人工备注")
        self.assertEqual(db._load_accounts()[0]["checkout_note"], "")

    def test_stale_worker_cannot_overwrite_new_run(self):
        self.start()
        self.save(finish=True)
        self.start("run-2")
        self.save([self.quote("JP", "0", "JPY")], run_id="run-2", status="running")
        before = copy.deepcopy(db.get_account(self.account_id))
        self.assertFalse(db.update_checkout_run(self.account_id, "run-1", status="failed", results=[], finish=True))
        self.assertFalse(db.update_account_plan_check(self.account_id, run_id="run-1", result={"ok": False, "error": "late"}))
        self.assertFalse(db.mark_account_plan_check_running(self.account_id, run_id="run-1"))
        self.assertEqual(db.get_account(self.account_id), before)

    def test_checkout_failure_does_not_change_successful_plan_result(self):
        self.start()
        before = db.get_account(self.account_id)
        self.save([{"country": "IN", "status": "failed", "error": "promo rejected"}], status="failed", finish=True)
        row = db.get_account(self.account_id)
        for field in ("plan_check_status", "plan_check_ok", "plan_check_error", "current_plan_type",
                      "plus_trial_eligible", "plan_check_result_json", "plan_last_success_result_json"):
            self.assertEqual(row.get(field), before.get(field), field)
        self.assertEqual(row["checkout_status"], "failed")
        self.assertEqual(row["plan_run_status"], "finished")

    def test_plan_lock_stays_held_while_quotes_are_running(self):
        self.start()
        self.save(status="running")
        self.assertFalse(db.claim_account_plan_check(self.account_id, run_id="run-2"))
        self.save(finish=True)
        self.assertTrue(db.claim_account_checkout(self.account_id, run_id="run-2"))

    def test_qualification_does_not_change_plan_when_it_has_not_been_checked(self):
        row = db.get_account(self.account_id)
        before = {k: row.get(k) for k in ("plan_check_status", "plan_check_ok", "current_plan_type")}
        self.assertTrue(db.claim_account_checkout(self.account_id, "independent-run"))
        self.assertTrue(db.mark_account_checkout_running(self.account_id, "independent-run"))
        self.save([self.quote()], run_id="independent-run", finish=True)
        after = db.get_account(self.account_id)
        self.assertEqual({k: after.get(k) for k in before}, before)

    def test_plan_lifecycle_does_not_touch_previous_checkout(self):
        self.start()
        self.save(finish=True)
        before = db.get_account(self.account_id)
        self.assertTrue(db.claim_account_plan_check(self.account_id, run_id="plan-run"))
        self.assertTrue(db.mark_account_plan_check_running(self.account_id, run_id="plan-run"))
        self.assertTrue(db.update_plan_run_lifecycle(self.account_id, "plan-run"))
        self.assertTrue(db.update_plan_run_lifecycle(self.account_id, "plan-run", finish=True))
        after = db.get_account(self.account_id)
        for key in ("checkout_status", "checkout_quotes", "checkout_note", "checkout_revision"):
            self.assertEqual(after.get(key), before.get(key), key)
        self.assertEqual(after["plan_run_status"], "finished")

    def test_restart_keeps_completed_country_and_interrupts_remaining(self):
        self.start()
        complete = self.quote()
        self.save([complete, {"country": "JP", "status": "running"},
                   {"country": "US", "status": "queued"}], status="running")
        before = db.get_account(self.account_id)
        self.assertEqual(before["checkout_progress_completed"], 1)
        self.assertGreater(db.recover_interrupted_plan_checks(), 0)
        row = db.get_account(self.account_id)
        results = row["checkout_quotes"]["results"]
        self.assertEqual(results[0], complete)
        self.assertEqual([r["status"] for r in results], ["success", "interrupted", "interrupted"])
        self.assertEqual(row["plan_check_status"], "success")
        self.assertEqual(row["plan_run_status"], "interrupted")
        self.assertEqual(row["checkout_status"], "interrupted")
        self.assertEqual(row["checkout_progress_completed"], 3)
        self.assertGreater(row["checkout_revision"], before["checkout_revision"])
        self.assertEqual(row["note"], "")
        self.assertEqual(row["checkout_zero_quotes"][0]["country"], "IN")
        self.assertFalse(db.update_checkout_run(self.account_id, "run-1", status="success", results=[complete], finish=True))

    def test_restart_does_not_rewrite_stale_results_from_previous_run(self):
        self.start()
        self.save(finish=True)
        old = db.get_account(self.account_id)["checkout_quotes"]
        self.assertTrue(db.claim_account_checkout(self.account_id, run_id="run-2"))
        self.assertGreater(db.recover_interrupted_plan_checks(), 0)
        row = db.get_account(self.account_id)
        self.assertEqual(row["checkout_quotes"], old)
        self.assertTrue(row["checkout_results_stale"])
        self.assertEqual(row["checkout_note"], "")
        self.assertEqual(row["checkout_zero_quotes"], [])

    def test_poll_revision_changes_in_same_second_and_returns_empty_notes(self):
        self.start()
        self.save(status="running")
        first = db.list_account_plan_check_statuses()
        self.assertTrue(db.update_checkout_run(self.account_id, "run-1", status="running", reason="next country"))
        second = db.list_account_plan_check_statuses()
        self.assertNotEqual(first["revision"], second["revision"])
        self.assertTrue(db.update_account_note(self.account_id, "manual"))
        third = db.list_account_plan_check_statuses()
        self.assertNotEqual(second["revision"], third["revision"])
        self.assertTrue(db.update_account_note(self.account_id, ""))
        self.assertTrue(db.update_checkout_run(self.account_id, "run-1", status="skipped", reason="", results=[], finish=True))
        cleared = db.list_account_plan_check_statuses()
        self.assertNotEqual(third["revision"], cleared["revision"])
        item = cleared["items"][0]
        for field in ("note", "manual_note", "checkout_note", "checkout_reason"):
            self.assertIn(field, item)
            self.assertEqual(item[field], "")
        self.assertNotIn("access_token", item)
        self.assertNotIn("checkout_quotes", item)


if __name__ == "__main__":
    unittest.main()
