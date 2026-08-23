import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from core import extract_link_service as service


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response):
        self.response = response
        self.urls = []

    def post(self, url, **kwargs):
        self.urls.append(("POST", url, kwargs.get("json")))
        return self.response

    def get(self, url, **kwargs):
        self.urls.append(("GET", url, kwargs.get("json")))
        return self.response

    def close(self):
        pass


class ExtractLinkServiceTests(unittest.TestCase):
    def test_fastapi_detail_is_mapped_without_http_status(self):
        exc = service._api_error(status_code=422, payload={"detail": "zero_trial_ineligible"})
        self.assertEqual(exc.code, "zero_trial_ineligible")
        self.assertEqual(str(exc), "GCash 不符合当前 0 元试用资格")
        self.assertNotEqual(str(exc), "HTTP 422")

    def test_nested_task_failure_is_mapped(self):
        payload = {"task": {"status": "failed", "failure": {
            "code": "gcash_payment_method_unavailable",
            "message": "GCash is unavailable for this checkout",
        }}}
        self.assertEqual(
            service._extract_error_message(payload),
            "当前优惠结账不支持 GCash",
        )

    def test_create_job_uses_extractions_without_zero_trial_endpoint(self):
        session = _Session(_Response({"task_id": "task-1"}))
        with patch.object(service, "_session", return_value=session):
            result = service._create_extract_job(
                token="access-token", link_type="gcash", cdk="UPI-test"
            )
        self.assertEqual(result["job_id"], "task-1")
        self.assertEqual(len(session.urls), 1)
        method, url, payload = session.urls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/extractions"))
        self.assertEqual(payload["link_type"], "gcash")
        self.assertEqual(payload["eligibility_proof"], "")
        self.assertFalse(any("check-zero-trial" in item[1] for item in session.urls))

    def test_requests_422_raises_readable_api_error(self):
        session = _Session(_Response({"detail": "eligibility_check_unavailable"}, 503))
        with patch.object(service, "_session", return_value=session):
            with self.assertRaises(service.ExtractLinkApiError) as ctx:
                service._create_extract_job(
                    token="access-token", link_type="gcash", cdk="UPI-test"
                )
        self.assertEqual(ctx.exception.code, "eligibility_check_unavailable")
        self.assertIn("暂时不可用", str(ctx.exception))

    def test_urllib_http_error_reads_response_body(self):
        body = io.BytesIO(json.dumps({"detail": "zero_trial_ineligible"}).encode())
        http_error = HTTPError("https://example.test/api/extractions", 422, "bad", {}, body)
        with patch.object(service, "_session", return_value=None), patch.object(
            service, "urlopen", side_effect=http_error
        ):
            with self.assertRaises(service.ExtractLinkApiError) as ctx:
                service._create_extract_job(
                    token="access-token", link_type="gcash", cdk="UPI-test"
                )
        self.assertEqual(ctx.exception.code, "zero_trial_ineligible")
        self.assertEqual(str(ctx.exception), "GCash 不符合当前 0 元试用资格")


if __name__ == "__main__":
    unittest.main()
