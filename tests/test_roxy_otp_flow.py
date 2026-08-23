import unittest
from unittest.mock import patch

import core.roxy_registration as registration


class _Driver:
    current_url = "https://chatgpt.com/"


class OtpFlowTests(unittest.TestCase):
    def test_wait_accepts_navigation_after_transient_otp_state(self):
        driver = _Driver()
        # The first observation is still the OTP page; the final observation is
        # the ChatGPT home page after the asynchronous navigation completes.
        with patch.object(registration, "_is_email_verification_page", side_effect=[True, False]), \
             patch.object(registration.time, "sleep"), \
             patch.object(registration.time, "time", side_effect=[0.0, 0.0, 1.0]):
            outcome = registration._wait_after_email_otp_submit(driver, timeout=0.1)

        self.assertEqual(outcome, "accepted")

    def test_resend_is_noop_when_otp_page_has_already_closed(self):
        driver = _Driver()
        with patch.object(registration, "_is_email_verification_page", return_value=False):
            result = registration._click_resend_email_otp(driver, timeout=1)

        self.assertEqual(result, {"ok": True, "reason": "already_left_otp"})


if __name__ == "__main__":
    unittest.main()
